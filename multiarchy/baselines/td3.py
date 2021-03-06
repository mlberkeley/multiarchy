"""Author: Brandon Trabucco, Copyright 2019, MIT License"""


from multiarchy.envs.normalized_env import NormalizedEnv
from multiarchy.distributions.gaussian import Gaussian
from multiarchy.networks import dense
from multiarchy.agents.policy_agent import PolicyAgent
from multiarchy.replay_buffers.step_replay_buffer import StepReplayBuffer
from multiarchy.loggers.tensorboard_logger import TensorboardLogger
from multiarchy.samplers.parallel_sampler import ParallelSampler
from multiarchy.savers.local_saver import LocalSaver
from multiarchy.algorithms.td3 import TD3
import numpy as np


td3_variant = dict(
    max_num_steps=1000000,
    logging_dir="./",
    hidden_size=400,
    num_hidden_layers=2,
    exploration_noise_std=0.1,
    reward_scale=1.0,
    discount=0.99,
    target_clipping=0.5,
    target_noise=0.2,
    policy_learning_rate=0.0003,
    qf_learning_rate=0.0003,
    tau=0.005,
    batch_size=256,
    max_path_length=1000,
    num_warm_up_steps=10000,
    num_steps_per_epoch=1000,
    num_steps_per_eval=10000,
    num_epochs_per_eval=1,
    num_epochs=10000)


def td3(
        variant,
        env_class,
        env_kwargs=None,
        observation_key="observation",
):
    # run an experiment with multiple agents
    if env_kwargs is None:
        env_kwargs = {}

    # initialize the environment to track the cardinality of actions
    env = NormalizedEnv(env_class, **env_kwargs)
    action_dim = env.action_space.low.size
    observation_dim = env.observation_space.spaces[
        observation_key].low.size

    # create a replay buffer to store data
    replay_buffer = StepReplayBuffer(
        max_num_steps=variant["max_num_steps"])

    # create a logging instance
    logger = TensorboardLogger(
        replay_buffer, variant["logging_dir"])

    # create policies for each level in the hierarchy
    policy = Gaussian(
        dense(
            observation_dim,
            action_dim,
            output_activation="tanh",
            hidden_size=variant["hidden_size"],
            num_hidden_layers=variant["num_hidden_layers"]),
        optimizer_kwargs=dict(learning_rate=variant["policy_learning_rate"]),
        tau=variant["tau"],
        std=variant["exploration_noise_std"])
    target_policy = policy.clone()

    qf1 = Gaussian(
        dense(
            observation_dim + action_dim,
            1,
            hidden_size=variant["hidden_size"],
            num_hidden_layers=variant["num_hidden_layers"]),
        optimizer_kwargs=dict(learning_rate=variant["qf_learning_rate"]),
        tau=variant["tau"],
        std=1.0)
    target_qf1 = qf1.clone()

    qf2 = Gaussian(
        dense(
            observation_dim + action_dim,
            1,
            hidden_size=variant["hidden_size"],
            num_hidden_layers=variant["num_hidden_layers"]),
        optimizer_kwargs=dict(learning_rate=variant["qf_learning_rate"]),
        tau=variant["tau"],
        std=1.0)
    target_qf2 = qf2.clone()

    # train the agent using soft actor critic
    algorithm = TD3(
        policy,
        target_policy,
        qf1,
        qf2,
        target_qf1,
        target_qf2,
        replay_buffer,
        reward_scale=variant["reward_scale"],
        discount=variant["discount"],
        target_clipping=variant["target_clipping"],
        target_noise=variant["target_noise"],
        observation_key=observation_key,
        batch_size=variant["batch_size"],
        logger=logger,
        logging_prefix="td3/")

    # create a single agent to manage the hierarchy
    agent = PolicyAgent(
        policy,
        algorithm=algorithm,
        observation_key=observation_key)

    # create a saver to record training progress to the disk
    saver = LocalSaver(
        replay_buffer,
        variant["logging_dir"],
        policy=policy,
        target_policy=target_policy,
        qf1=qf1,
        qf2=qf2,
        target_qf1=target_qf1,
        target_qf2=target_qf2)

    # load the networks if already trained
    saver.load()

    # make a sampler to collect data to warm up the hierarchy
    sampler = ParallelSampler(
        env,
        agent,
        max_path_length=variant["max_path_length"],
        num_workers=variant["num_workers"])

    # collect more training samples
    sampler.set_weights(agent.get_weights())
    paths, returns, num_steps = sampler.collect(
        variant["num_warm_up_steps"],
        deterministic=False,
        keep_data=True,
        workers_to_use=variant["num_workers"])

    # insert the samples into the replay buffer
    for o, a, r in paths:
        replay_buffer.insert_path(o, a, r)

    #  train for a specified number of iterations
    for iteration in range(variant["num_epochs"]):

        if iteration % variant["num_epochs_per_eval"] == 0:
            # evaluate the policy at this step
            sampler.set_weights(agent.get_weights())
            paths, eval_returns, num_steps = sampler.collect(
                variant["num_steps_per_eval"],
                deterministic=True,
                keep_data=False,
                workers_to_use=variant["num_workers"])
            logger.record("eval_mean_return", np.mean(eval_returns))

            # save the replay buffer and the policies
            saver.save()

        # collect more training samples
        sampler.set_weights(agent.get_weights())
        paths, train_returns, num_steps = sampler.collect(
            variant["num_steps_per_epoch"],
            deterministic=False,
            keep_data=True,
            workers_to_use=1)
        logger.record("train_mean_return", np.mean(train_returns))

        # insert the samples into the replay buffer
        for o, a, r in paths:
            replay_buffer.insert_path(o, a, r)

        # train once each for the number of steps collected
        for i in range(num_steps):
            agent.train()
