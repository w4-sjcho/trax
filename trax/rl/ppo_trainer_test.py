# coding=utf-8
# Copyright 2020 The Trax Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tests for trax.rl.ppo's training_loop."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import contextlib
import functools
import itertools
import os
import tempfile

from absl.testing import parameterized
import gin
import gym
import numpy as np

from tensor2tensor.envs import gym_env_problem
from tensor2tensor.rl import gym_utils
from tensorflow import test
from tensorflow.compat.v1.io import gfile
from trax import layers
from trax import lr_schedules as lr
from trax import math
from trax import models
from trax import optimizers as trax_opt
from trax.rl import envs  # pylint: disable=unused-import
from trax.rl import ppo_trainer
from trax.rl import serialization_utils
from trax.rl import simulated_env_problem
from trax.rl import space_serializer
from trax.supervised import inputs as trax_inputs
from trax.supervised import trainer_lib


class PpoTrainerTest(parameterized.TestCase):

  def get_wrapped_env(
      self, name='CartPole-v0', max_episode_steps=2, batch_size=1
  ):
    wrapper_fn = functools.partial(
        gym_utils.gym_env_wrapper,
        **{
            'rl_env_max_episode_steps': max_episode_steps,
            'maxskip_env': False,
            'rendered_env': False,
            'rendered_env_resize_to': None,  # Do not resize frames
            'sticky_actions': False,
            'output_dtype': None,
            'num_actions': None,
        })

    return gym_env_problem.GymEnvProblem(base_env_name=name,
                                         batch_size=batch_size,
                                         env_wrapper_fn=wrapper_fn,
                                         discrete_rewards=False)

  @contextlib.contextmanager
  def tmp_dir(self):
    tmp = tempfile.mkdtemp()
    yield tmp
    gfile.rmtree(tmp)

  def _make_trainer(
      self, train_env, eval_env, output_dir, model=None, **kwargs):
    if model is None:
      model = lambda: layers.Serial(layers.Dense(1))
    return ppo_trainer.PPO(
        train_env=train_env,
        eval_env=eval_env,
        policy_and_value_model=model,
        n_optimizer_steps=1,
        output_dir=output_dir,
        random_seed=0,
        max_timestep=3,
        boundary=2,
        save_every_n=1,
        **kwargs
    )

  def test_training_loop_cartpole(self):
    with self.tmp_dir() as output_dir:
      trainer = self._make_trainer(
          train_env=self.get_wrapped_env('CartPole-v0', 2),
          eval_env=self.get_wrapped_env('CartPole-v0', 2),
          output_dir=output_dir,
      )
      trainer.training_loop(n_epochs=2)

  def test_training_loop_cartpole_transformer(self):
    with self.tmp_dir() as output_dir:
      trainer = self._make_trainer(
          train_env=self.get_wrapped_env('CartPole-v0', 2),
          eval_env=self.get_wrapped_env('CartPole-v0', 2),
          output_dir=output_dir,
          model=functools.partial(
              models.TransformerDecoder,
              d_model=1,
              d_ff=1,
              n_layers=1,
              n_heads=1,
              max_len=128,
              mode='train',
          ),
      )
      trainer.training_loop(n_epochs=2)

  def test_training_loop_onlinetune(self):
    with self.tmp_dir() as output_dir:
      gin.bind_parameter('OnlineTuneEnv.model', functools.partial(
          models.MLP,
          n_hidden_layers=0,
          n_output_classes=1,
      ))
      gin.bind_parameter('OnlineTuneEnv.inputs', functools.partial(
          trax_inputs.random_inputs,
          input_shape=(1, 1),
          input_dtype=np.float32,
          output_shape=(1, 1),
          output_dtype=np.float32,
      ))
      gin.bind_parameter('OnlineTuneEnv.train_steps', 1)
      gin.bind_parameter('OnlineTuneEnv.eval_steps', 1)
      gin.bind_parameter(
          'OnlineTuneEnv.output_dir', os.path.join(output_dir, 'envs'))
      trainer = self._make_trainer(
          train_env=self.get_wrapped_env('OnlineTuneEnv-v0', 1),
          eval_env=self.get_wrapped_env('OnlineTuneEnv-v0', 1),
          output_dir=output_dir,
      )
      trainer.training_loop(n_epochs=1)

  def test_training_loop_simulated(self):
    n_actions = 5
    history_shape = (3, 2, 3)
    action_shape = (3,)
    obs_shape = (3, 3)
    reward_shape = (3, 1)

    def model(mode):
      del mode
      return layers.Serial(
          layers.Parallel(
              layers.Flatten(),  # Observation stack.
              layers.Embedding(d_feature=1, vocab_size=n_actions),  # Action.
          ),
          layers.Concatenate(),
          layers.Dense(n_units=1),
          layers.Dup(),
          layers.Parallel(
              layers.Dense(n_units=obs_shape[1]),  # New observation.
              None,  # Reward.
          )
      )

    stream = itertools.repeat(
        (np.zeros(history_shape), np.zeros(action_shape, dtype=np.int32),
         np.zeros(obs_shape), np.zeros(reward_shape))
    )
    inp = trax_inputs.Inputs(lambda _: stream)
    inp._input_shape = (history_shape[1:], action_shape[1:])
    inp._input_dtype = (np.float32, np.int32)
    inp._target_shape = (obs_shape[1:], reward_shape[1:])
    inp._target_dtype = (np.float32, np.float32)
    inputs = inp

    def loss():
      """Cross-entropy loss as scalar compatible with Trax masking."""
      ones = layers.Fn(lambda x: math.numpy.ones_like(x))  # pylint: disable=unnecessary-lambda
      return layers.Serial(
          # Swap from (pred-obs, pred-reward, target-obs, target-reward)
          # to (pred-obs, target-obs, pred-reward, target-reward).
          layers.Parallel([], layers.Swap()),
          # Duplicate target-obs and target-reward and make 1 to add weights.
          layers.Parallel([], layers.Branch([], ones)),
          layers.Parallel([], [], [], [], layers.Branch([], ones)),
          # Cross-entropy loss for obs, L2 loss on reward.
          layers.Parallel(layers.CrossEntropyLoss(),
                          layers.L2Loss()),
          # Add both losses.
          layers.Add(),
          # Zero out in this test.
          layers.Fn(lambda x: x * 0.0),
      )

    with self.tmp_dir() as output_dir:
      # Run fake training just to save the parameters.
      trainer = trainer_lib.Trainer(
          model=model,
          loss_fn=loss(),
          inputs=inputs,
          optimizer=trax_opt.SM3,
          lr_schedule=lr.MultifactorSchedule,
          output_dir=output_dir,
      )
      trainer.train_epoch(n_steps=1, n_eval_steps=1)

      # Repeat the history over and over again.
      stream = itertools.repeat(np.zeros(history_shape))
      env_fn = functools.partial(
          simulated_env_problem.RawSimulatedEnvProblem,
          model=model,
          history_length=history_shape[1],
          trajectory_length=3,
          batch_size=history_shape[0],
          observation_space=gym.spaces.Box(
              low=-np.inf, high=np.inf, shape=(obs_shape[1],)),
          action_space=gym.spaces.Discrete(n=n_actions),
          reward_range=(-1, 1),
          discrete_rewards=False,
          history_stream=stream,
          output_dir=output_dir,
      )

      trainer = self._make_trainer(
          train_env=env_fn(),
          eval_env=env_fn(),
          output_dir=output_dir,
      )
      trainer.training_loop(n_epochs=2)

  def test_restarts(self):
    with self.tmp_dir() as output_dir:
      train_env = self.get_wrapped_env('CartPole-v0', 2)
      eval_env = self.get_wrapped_env('CartPole-v0', 2)

      # Train for 1 epoch and save.
      trainer = self._make_trainer(
          train_env=train_env,
          eval_env=eval_env,
          output_dir=output_dir,
      )
      self.assertEqual(trainer.epoch, 0)
      trainer.training_loop(n_epochs=1)
      self.assertEqual(trainer.epoch, 1)

      # Initialize with the same `output_dir`.
      trainer = self._make_trainer(
          train_env=train_env,
          eval_env=eval_env,
          output_dir=output_dir,
      )
      # reset the trainer manually and check that it initializes.
      trainer.reset()
      self.assertEqual(trainer.epoch, 1)
      # Check that we can continue training from the restored checkpoint.
      trainer.training_loop(n_epochs=2)
      self.assertEqual(trainer.epoch, 2)

  def test_training_loop_multi_control(self):
    gym.register(
        'FakeEnv-v0',
        entry_point='trax.rl.envs.fake_env:FakeEnv',
        kwargs={'n_actions': 3, 'n_controls': 2},
    )
    with self.tmp_dir() as output_dir:
      trainer = self._make_trainer(
          train_env=self.get_wrapped_env('FakeEnv-v0', 2),
          eval_env=self.get_wrapped_env('FakeEnv-v0', 2),
          output_dir=output_dir,
      )
      trainer.training_loop(n_epochs=2)

  def test_training_loop_cartpole_serialized(self):
    gin.bind_parameter('BoxSpaceSerializer.precision', 1)
    with self.tmp_dir() as output_dir:
      trainer = self._make_trainer(
          train_env=self.get_wrapped_env('CartPole-v0', 2),
          eval_env=self.get_wrapped_env('CartPole-v0', 2),
          output_dir=output_dir,
          model=functools.partial(
              models.TransformerDecoder,
              d_model=1,
              d_ff=1,
              n_layers=1,
              n_heads=1,
              max_len=1024,
              mode='train',
          ),
          policy_and_value_vocab_size=4,
      )
      trainer.training_loop(n_epochs=2)

  @parameterized.named_parameters(('two_towers', True), ('one_tower', False))
  def test_training_loop_cartpole_serialized_init_from_world_model(
      self, two_towers
  ):
    gin.bind_parameter('BoxSpaceSerializer.precision', 1)

    transformer_kwargs = {
        'd_model': 1,
        'd_ff': 1,
        'n_layers': 1,
        'n_heads': 1,
        'max_len': 128,
    }
    obs_serializer = space_serializer.create(
        gym.spaces.MultiDiscrete([2, 2]), vocab_size=4
    )
    act_serializer = space_serializer.create(
        gym.spaces.Discrete(2), vocab_size=4
    )
    model_fn = lambda mode: serialization_utils.SerializedModel(  # pylint: disable=g-long-lambda
        seq_model=models.TransformerLM(
            mode=mode, vocab_size=4, **transformer_kwargs
        ),
        observation_serializer=obs_serializer,
        action_serializer=act_serializer,
        significance_decay=0.9,
    )
    with self.tmp_dir() as output_dir:
      model_dir = os.path.join(output_dir, 'model')

      def dummy_stream(_):
        while True:
          obs = np.zeros((1, 2, 2), dtype=np.int32)
          act = np.zeros((1, 1), dtype=np.int32)
          mask = np.ones_like(obs)
          yield (obs, act, obs, mask)

      inputs = trax_inputs.Inputs(
          train_stream=dummy_stream, eval_stream=dummy_stream
      )
      inputs._input_shape = ((2, 2), (1,))  # pylint: disable=protected-access
      inputs._input_dtype = (np.int32, np.int32)  # pylint: disable=protected-access

      # Initialize a world model checkpoint by running the trainer.
      trainer_lib.train(
          model_dir,
          model=model_fn,
          inputs=inputs,
          steps=1,
          eval_steps=1,
      )

      policy_dir = os.path.join(output_dir, 'policy')
      trainer = self._make_trainer(
          train_env=self.get_wrapped_env('CartPole-v0', 2),
          eval_env=self.get_wrapped_env('CartPole-v0', 2),
          output_dir=policy_dir,
          model=functools.partial(
              models.TransformerDecoder, **transformer_kwargs
          ),
          policy_and_value_vocab_size=4,
          init_policy_from_world_model_output_dir=model_dir,
          policy_and_value_two_towers=two_towers,
      )
      trainer.training_loop(n_epochs=2)

  def test_training_loop_cartpole_minibatch(self):
    with self.tmp_dir() as output_dir:
      trainer = self._make_trainer(
          train_env=self.get_wrapped_env('CartPole-v0', 2, batch_size=4),
          eval_env=self.get_wrapped_env('CartPole-v0', 2),
          output_dir=output_dir,
          optimizer_batch_size=2,
      )
      trainer.training_loop(n_epochs=2)


if __name__ == '__main__':
  test.main()
