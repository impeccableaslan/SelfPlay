"""
A simple version of Distributed Proximal Policy Optimization (DPPO) using distributed TensorFlow.
Based on:
1. Emergence of Locomotion Behaviours in Rich Environments (Google Deepmind): [https://arxiv.org/abs/1707.02286]
2. Proximal Policy Optimization Algorithms (OpenAI): [https://arxiv.org/abs/1707.06347]
3. Generalized Advantage Estimation [https://arxiv.org/abs/1506.02438]
Thanks to OpenAI and morvanzhou for their examples
"""
import sys
sys.path.append("~/Desktop/Mods/ai/projects/selfplay")
import argparse
import tensorflow as tf
import numpy as np
import gym
import os
import scipy.signal
from time import time
from gym import wrappers
from tensorflow.python.training.summary_io import SummaryWriterCache
from utils import RunningStats, discount, add_histogram
OUTPUT_RESULTS_DIR = ".\\outputs"


EP_MAX = 1000
GAMMA = 0.99
LAMBDA = 0.95
ENTROPY_BETA = 0.0  # 0.01 for discrete, 0.0 for continuous
LR = 0.0001
BATCH = 8192  # 128 for discrete, 8192 for continuous
MINIBATCH = 32
EPOCHS = 10
EPSILON = 0.1
VF_COEFF = 1.0
L2_REG = 0.001
SIGMA_FLOOR = 0.0
PORT_OFFSET = 140  # number to offset the port from 2222. Useful for multiple runs


class PPO(object):
    def __init__(self, environment, wid, greyscale=True):
        if len(environment.action_space.shape) > 0:
            self.discrete = False
            self.s_dim, self.a_dim = environment.observation_space.shape, environment.action_space.shape[0]
            self.a_bound = (environment.action_space.high - environment.action_space.low) / 2
            self.actions = tf.placeholder(tf.float32, [None, self.a_dim], 'action')
        else:
            self.discrete = True
            self.s_dim, self.a_dim = environment.observation_space.shape, environment.action_space.n
            self.actions = tf.placeholder(tf.int32, [None, 1], 'action')
        self.cnn = len(self.s_dim) == 3
        self.greyscale = greyscale  # If not greyscale and using RGB, make sure to divide the images by 255
        self.wid = wid
        is_chief = wid == 0

        self.state = tf.placeholder(tf.float32, [None] + list(self.s_dim), 'state')
        self.advantage = tf.placeholder(tf.float32, [None, 1], 'advantage')
        self.rewards = tf.placeholder(tf.float32, [None, 1], 'discounted_r')

        self.dataset = tf.data.Dataset.from_tensor_slices({"state": self.state, "actions": self.actions,
                                                           "rewards": self.rewards, "advantage": self.advantage})
        self.dataset = self.dataset.shuffle(buffer_size=10000)
        self.dataset = self.dataset.batch(MINIBATCH)
        self.dataset = self.dataset.cache()
        self.dataset = self.dataset.repeat(EPOCHS)
        self.iterator = self.dataset.make_initializable_iterator()
        batch = self.iterator.get_next()

        pi_old, pi_old_params = self._build_anet(batch["state"], 'oldpi')
        pi, pi_params = self._build_anet(batch["state"], 'pi')
        pi_eval, _ = self._build_anet(self.state, 'pi', reuse=True)

        vf_old, vf_old_params = self._build_cnet(batch["state"], "oldvf")
        self.v, vf_params = self._build_cnet(batch["state"], "vf")
        self.vf_eval, _ = self._build_cnet(self.state, 'vf', reuse=True)

        self.sample_op = tf.squeeze(pi_eval.sample(1), axis=0, name="sample_action")
        self.eval_action = pi_eval.mean()  # Used mode for discrete case. Mode should equal mean in continuous
        self.global_step = tf.train.get_or_create_global_step()

        with tf.variable_scope('loss'):
            epsilon_decay = tf.train.polynomial_decay(EPSILON, self.global_step, 1e5, 0.01, power=0.0)

            with tf.variable_scope('policy'):
                # Use floor functions for the probabilities to prevent NaNs when prob = 0
                ratio = tf.maximum(pi.prob(batch["actions"]), 1e-6) / tf.maximum(pi_old.prob(batch["actions"]), 1e-6)
                ratio = tf.clip_by_value(ratio, 0, 10)
                surr1 = batch["advantage"] * ratio
                surr2 = batch["advantage"] * tf.clip_by_value(ratio, 1 - epsilon_decay, 1 + epsilon_decay)
                loss_pi = -tf.reduce_mean(tf.minimum(surr1, surr2))
                tf.summary.scalar("loss", loss_pi)

            with tf.variable_scope('value_function'):
                # Sometimes values clipping helps, sometimes just using raw residuals is better ¯\_(ツ)_/¯
                clipped_value_estimate = vf_old + tf.clip_by_value(self.v - vf_old, -epsilon_decay, epsilon_decay)
                loss_vf1 = tf.squared_difference(clipped_value_estimate, batch["rewards"])
                loss_vf2 = tf.squared_difference(self.v, batch["rewards"])
                loss_vf = tf.reduce_mean(tf.maximum(loss_vf1, loss_vf2)) * 0.5
                # loss_vf = tf.reduce_mean(tf.square(self.v - batch["rewards"])) * 0.5
                tf.summary.scalar("loss", loss_vf)

            with tf.variable_scope('entropy'):
                entropy = pi.entropy()
                pol_entpen = -ENTROPY_BETA * tf.reduce_mean(entropy)

            loss = loss_pi + loss_vf * VF_COEFF + pol_entpen
            tf.summary.scalar("total", loss)
            # tf.summary.scalar("epsilon", epsilon_decay)

        with tf.variable_scope('train'):
            self.global_step = tf.train.get_or_create_global_step()
            opt = tf.train.AdamOptimizer(LR)
            opt = tf.train.SyncReplicasOptimizer(opt, replicas_to_aggregate=N_AGG, total_num_replicas=N_WORKER)

            self.train_op = opt.minimize(loss, global_step=self.global_step, var_list=pi_params + vf_params)

            # grads, vs = zip(*opt.compute_gradients(loss, var_list=pi_params + vf_params))
            # Need to split the two networks so that clip_by_global_norm works properly
            # pi_grads, pi_vs = grads[:len(pi_params)], vs[:len(pi_params)]
            # vf_grads, vf_vs = grads[len(pi_params):], vs[len(pi_params):]
            # pi_grads, _ = tf.clip_by_global_norm(pi_grads, 10.0)
            # vf_grads, _ = tf.clip_by_global_norm(vf_grads, 10.0)
            # self.train_op = opt.apply_gradients(zip(pi_grads + vf_grads, pi_vs + vf_vs), global_step=self.global_step)

            self.sync_replicas_hook = opt.make_session_run_hook(is_chief)

        with tf.variable_scope('update_old'):
            self.update_pi_old_op = [oldp.assign(p) for p, oldp in zip(pi_params, pi_old_params)]
            self.update_vf_old_op = [oldp.assign(p) for p, oldp in zip(vf_params, vf_old_params)]

        tf.summary.scalar("value", tf.reduce_mean(self.v))
        tf.summary.scalar("policy_entropy", tf.reduce_mean(entropy))
        if not self.discrete:
            tf.summary.scalar("sigma", tf.reduce_mean(pi.stddev()))
        self.summarise = tf.summary.merge(tf.get_collection(tf.GraphKeys.SUMMARIES))

    def _build_anet(self, state_in, name, reuse=False):
        w_reg = tf.contrib.layers.l2_regularizer(L2_REG)

        with tf.variable_scope(name, reuse=reuse):
            if self.cnn:
                if self.greyscale:
                    state_in = tf.image.rgb_to_grayscale(state_in)
                conv1 = tf.layers.conv2d(inputs=state_in, filters=32, kernel_size=8, strides=4, activation=tf.nn.relu)
                conv2 = tf.layers.conv2d(inputs=conv1, filters=64, kernel_size=4, strides=2, activation=tf.nn.relu)
                conv3 = tf.layers.conv2d(inputs=conv2, filters=64, kernel_size=3, strides=1, activation=tf.nn.relu)
                state_in = tf.layers.flatten(conv3)

            layer_1 = tf.layers.dense(state_in, 400, tf.nn.relu, kernel_regularizer=w_reg, name="pi_l1")
            layer_2 = tf.layers.dense(layer_1, 400, tf.nn.relu, kernel_regularizer=w_reg, name="pi_l2")

            if self.discrete:
                a_logits = tf.layers.dense(layer_2, self.a_dim, kernel_regularizer=w_reg, name="pi_logits")
                dist = tf.distributions.Categorical(logits=a_logits)
            else:
                mu = tf.layers.dense(layer_2, self.a_dim, tf.nn.tanh, kernel_regularizer=w_reg, name="pi_mu")
                log_sigma = tf.get_variable(name="pi_sigma", shape=self.a_dim, initializer=tf.zeros_initializer())
                dist = tf.distributions.Normal(loc=mu * self.a_bound, scale=tf.maximum(tf.exp(log_sigma), SIGMA_FLOOR))
        params = tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES, scope=name)
        return dist, params

    def _build_cnet(self, state_in, name, reuse=False):
        w_reg = tf.contrib.layers.l2_regularizer(L2_REG)

        with tf.variable_scope(name, reuse=reuse):
            if self.cnn:
                if self.greyscale:
                    state_in = tf.image.rgb_to_grayscale(state_in)
                conv1 = tf.layers.conv2d(inputs=state_in, filters=32, kernel_size=8, strides=4, activation=tf.nn.relu)
                conv2 = tf.layers.conv2d(inputs=conv1, filters=64, kernel_size=4, strides=2, activation=tf.nn.relu)
                conv3 = tf.layers.conv2d(inputs=conv2, filters=64, kernel_size=3, strides=1, activation=tf.nn.relu)
                state_in = tf.layers.flatten(conv3)

            l1 = tf.layers.dense(state_in, 400, tf.nn.relu, kernel_regularizer=w_reg, name="vf_l1")
            l2 = tf.layers.dense(l1, 400, tf.nn.relu, kernel_regularizer=w_reg, name="vf_l2")
            vf = tf.layers.dense(l2, 1, kernel_regularizer=w_reg, name="vf_output")

        params = tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES, scope=name)
        return vf, params

    def update(self, s, a, r, adv, sess):
        start = time()
        e_time = []

        sess.run([self.update_pi_old_op, self.update_vf_old_op, self.iterator.initializer],
                 feed_dict={self.state: s, self.actions: a, self.rewards: r, self.advantage: adv})

        while True:
            try:
                e_start = time()
                summary, step, _ = sess.run([self.summarise, self.global_step, self.train_op])
                e_time.append(time() - e_start)
            except tf.errors.OutOfRangeError:
                break
        print("Worker_%i Trained in %.3fs at %.3fs/batch. Global step %i" % (self.wid, time() - start, np.mean(e_time), step))
        return summary

    def evaluate_state(self, state, sess, stochastic=True):
        if stochastic:
            action, value = sess.run([self.sample_op, self.vf_eval], {self.state: state[np.newaxis, :]})
        else:
            action, value = sess.run([self.eval_action, self.vf_eval], {self.state: state[np.newaxis, :]})
        return action[0], np.squeeze(value)


def start_parameter_server(pid, spec):
    cluster = tf.train.ClusterSpec(spec)
    server = tf.train.Server(cluster, job_name="ps", task_index=pid)
    print("Starting PS #{}".format(pid))
    server.join()


# class StopAtEpisodeHook(tf.train.SessionRunHook):
#     def __init__(self, max_episodes, worker):
#         self.ep_max = max_episodes
#         self.wid = worker
#
#     def after_run(self, run_context, run_values):
#         global episode
#         if episode >= self.ep_max and self.wid == 0:
#             print("Reached %i episodes" % episode)
#             run_context.request_stop()


class Worker(object):
    def __init__(self, wid, spec):
        self.wid = wid
        self.env = gym.make(ENVIRONMENT)

        print("Starting Worker #{}".format(wid))
        cluster = tf.train.ClusterSpec(spec)
        self.server = tf.train.Server(cluster, job_name="worker", task_index=wid)

        with tf.device(tf.train.replica_device_setter(worker_device="/job:worker/task:%d" % wid, cluster=cluster)):
            if self.wid == 0:
                self.env = wrappers.Monitor(self.env, os.path.join(SUMMARY_DIR, ENVIRONMENT), video_callable=None)
            self.ppo = PPO(self.env, self.wid)

    def work(self):
        hooks = [self.ppo.sync_replicas_hook]
        sess = tf.train.MonitoredTrainingSession(master=self.server.target, is_chief=(self.wid == 0),
                                                 checkpoint_dir=SUMMARY_DIR,
                                                 save_summaries_steps=None, save_summaries_secs=None, hooks=hooks)
        if self.wid == 0:
            writer = SummaryWriterCache.get(SUMMARY_DIR)

        t, episode, terminal = 0, 0, False
        buffer_s, buffer_a, buffer_r, buffer_v, buffer_terminal = [], [], [], [], []
        rolling_r = RunningStats()

        while not sess.should_stop() and not (episode > EP_MAX and self.wid == 0):

            s = self.env.reset()
            ep_r, ep_t, ep_a = 0, 0, []

            while True:
                a, v = self.ppo.evaluate_state(s, sess)

                # Update ppo
                if t == BATCH:  # or (terminal and t < BATCH):
                    # Normalise rewards
                    rewards = np.array(buffer_r)
                    rolling_r.update(rewards)
                    rewards = np.clip(rewards / rolling_r.std, -10, 10)

                    v_final = [v * (1 - terminal)]  # v = 0 if terminal, otherwise use the predicted v
                    values = np.array(buffer_v + v_final)
                    terminals = np.array(buffer_terminal + [terminal])

                    # Generalized Advantage Estimation - https://arxiv.org/abs/1506.02438
                    delta = rewards + GAMMA * values[1:] * (1 - terminals[1:]) - values[:-1]
                    advantage = discount(delta, GAMMA * LAMBDA, terminals)
                    returns = advantage + np.array(buffer_v)
                    advantage = (advantage - advantage.mean()) / np.maximum(advantage.std(), 1e-6)

                    bs, ba, br, badv = np.reshape(buffer_s, (t,) + self.ppo.s_dim), np.vstack(buffer_a), \
                                       np.vstack(returns), np.vstack(advantage)

                    graph_summary = self.ppo.update(bs, ba, br, badv, sess)
                    buffer_s, buffer_a, buffer_r, buffer_v, buffer_terminal = [], [], [], [], []
                    t = 0

                buffer_s.append(s)
                buffer_a.append(a)
                buffer_v.append(v)
                buffer_terminal.append(terminal)
                ep_a.append(a)

                if not self.ppo.discrete:
                    a = np.clip(a, self.env.action_space.low, self.env.action_space.high)
                s, r, terminal, _ = self.env.step(a)
                buffer_r.append(r)

                ep_r += r
                ep_t += 1
                t += 1

                if terminal:
                    # End of episode summary
                    print('Worker_%i' % self.wid,
                          '| Episode: %i' % episode, "| Reward: %.2f" % ep_r, '| Steps: %i' % ep_t)

                    if self.wid == 0:
                        worker_summary = tf.Summary()
                        worker_summary.value.add(tag="Reward", simple_value=ep_r)

                        # Create Action histograms for each dimension
                        actions = np.array(ep_a)
                        if self.ppo.discrete:
                            add_histogram(writer, "Action", actions, episode, bins=self.ppo.a_dim)
                        else:
                            for a in range(self.ppo.a_dim):
                                add_histogram(writer, "Action/Dim" + str(a), actions[:, a], episode)

                        try:
                            writer.add_summary(graph_summary, episode)
                        except NameError:
                            pass
                        writer.add_summary(worker_summary, episode)
                        writer.flush()

                    episode += 1
                    break

        self.env.close()
        print("Worker_%i finished" % self.wid)


def main(_):
    pass


if __name__ == '__main__':
    # Discrete environments
    # ENVIRONMENT = 'CartPole-v1'
    # ENVIRONMENT = 'MountainCar-v0'
    # ENVIRONMENT = 'LunarLander-v2'
    ENVIRONMENT = 'Pendulum-v0'

    # Continuous environments
    # ENVIRONMENT = 'Pendulum-v0'
    # ENVIRONMENT = 'MountainCarContinuous-v0'
    # ENVIRONMENT = 'LunarLanderContinuous-v2'
    # ENVIRONMENT = 'BipedalWalker-v2'
    # ENVIRONMENT = 'BipedalWalkerHardcore-v2'
    # ENVIRONMENT = 'CarRacing-v0'

    os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'
    tf.logging.set_verbosity(tf.logging.ERROR)
    parser = argparse.ArgumentParser()
    parser.add_argument('--job_name', action='store', dest='job_name', help='Either "ps" or "worker"')
    parser.add_argument('--task_index', action='store', dest='task_index', help='ID number of the job')
    parser.add_argument('--timestamp', action='store', dest='timestamp', help='Timestamp for output directory')
    parser.add_argument('--workers', action='store', dest='n_workers', help='Number of workers')
    parser.add_argument('--agg', action='store', dest='n_agg', help='Number of gradients to aggregate')
    parser.add_argument('--ps', action='store', dest='ps', help='Number of parameter servers')
    args = parser.parse_args()

    N_WORKER = int(args.n_workers)
    N_AGG = int(args.n_agg)
    PS = int(args.ps)
    TIMESTAMP = str(args.timestamp)
    SUMMARY_DIR = os.path.join(OUTPUT_RESULTS_DIR, "DPPO", ENVIRONMENT, TIMESTAMP)

    # This could be defined in a parameter file
    if PS == 0:
        spec = {"worker": ["localhost:" + str(2222 + PS + i + PORT_OFFSET) for i in range(N_WORKER)]}
    else:
        spec = {"ps": ["localhost:" + str(2222 + i + PORT_OFFSET) for i in range(PS)],
                "worker": ["localhost:" + str(2222 + PS + i + PORT_OFFSET) for i in range(N_WORKER)]}

    if args.job_name == "ps":
        tf.app.run(start_parameter_server(int(args.task_index), spec))
    elif args.job_name == "worker":
        w = Worker(int(args.task_index), spec)
        tf.app.run(w.work())