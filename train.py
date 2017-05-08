import gym
import torch
from torch import nn
from torch.autograd import Variable

from memory import ReplayMemory
from model import ActorCritic
from utils import action_to_one_hot, extend_input, state_to_tensor


def _transfer_grads_to_shared_model(model, shared_model):
  for param, shared_param in zip(model.parameters(), shared_model.parameters()):
    if shared_param.grad is not None:
      return
    shared_param._grad = param.grad


# Linearly decays learning rate
def _decay_learning_rate(optimiser, steps):
  eps = 1e-32
  for param_group in optimiser.param_groups:
    param_group['lr'] = max(param_group['lr'] - param_group['lr'] / steps, eps)


def train(rank, args, T, shared_model, optimiser):
  torch.manual_seed(args.seed + rank)

  env = gym.make(args.env)
  env.seed(args.seed + rank)
  action_size = env.action_space.n
  model = ActorCritic(env.observation_space, env.action_space, args.hidden_size)
  model.train()

  memory = ReplayMemory(args.memory_capacity)

  t = 1  # Thread step counter
  done = True  # Start new episode

  while T.value() <= args.T_max:
    # Sync with shared model at least every t_max steps
    model.load_state_dict(shared_model.state_dict())
    # Get starting timestep
    t_start = t

    # Reset or pass on hidden state
    if done:
      hx = Variable(torch.zeros(1, args.hidden_size))
      cx = Variable(torch.zeros(1, args.hidden_size))
      # Reset environment and done flag
      state = state_to_tensor(env.reset())
      action, reward, done, episode_length = torch.LongTensor([0]).unsqueeze(0), 0, False, 0
    elif not args.no_truncate:
      # Perform truncated backpropagation-through-time (allows freeing buffers after backwards call)
      hx = Variable(hx.data)
      cx = Variable(cx.data)

    # Lists of outputs for training
    Qs, Vs, log_probs, rewards, entropies = [], [], [], [], []

    while not done and t - t_start < args.t_max:
      input = extend_input(state, action_to_one_hot(action, action_size), reward, episode_length)
      # Calculate policy and values
      policy, Q, (hx, cx) = model(input, (hx, cx))
      V = (policy * Q).sum(1)  # V is expectation of Q under π
      log_policy = policy.log()
      entropy = -(log_policy * policy).sum(1)

      # Sample action
      action = policy.multinomial().data
      log_prob = log_policy.gather(1, Variable(action))  # Graph broken as loss for stochastic action calculated manually

      # Step
      next_state, reward, done, _ = env.step(action[0, 0])
      next_state = state_to_tensor(next_state)
      reward = args.reward_clip and min(max(reward, -1), 1) or reward  # Optionally clamp rewards

      # Save outputs for training
      memory.push(state, action, next_state, reward, done)  # Save in memory
      Qs.append(Q)
      Vs.append(V)
      log_probs.append(log_prob)
      rewards.append(reward)
      entropies.append(entropy)

      # Increment counters
      t += 1
      T.increment()

      # Increase episode counter
      episode_length += 1
      done = done or episode_length >= args.max_episode_length
      state = next_state

    # Return R = 0 for terminal s or V(s_i; θ) for non-terminal s
    if done:
      R = torch.zeros(1, 1)
    else:
      policy, Q, _ = model(input, (hx, cx))
      policy, Q = policy.data, Q.data
      V = (policy * Q).sum(1)
      R = V
      # TODO: Check if the following is correct
      action = policy.multinomial().data
      log_prob = policy.log().gather(1, Variable(action))
      Qs.append(Variable(Q))
      log_probs.append(log_prob)
    Vs.append(Variable(R))

    # Train the network
    policy_loss = 0
    value_loss = 0
    R = Variable(R)
    A_GAE = torch.zeros(1, 1)  # Generalised advantage estimator Ψ
    Qrets = [None] * len(Vs)
    # Calculate n-step returns in forward view, stepping backwards from the last state
    for i in reversed(range(len(rewards))):
      # R ← r_i + γR
      R = rewards[i] + args.discount * R
      """
      # Advantage A = R - V(s_i; θ)
      A = R - Vs[i]
      # dθ ← dθ - ∂A^2/∂θ
      value_loss += A ** 2
      """

      # ρ_i+1 = π(a_i+1|s_i+1) / µ(a_i+1|s_i+1)
      if len(log_probs) <= i + 1:
        importance_weight = torch.FloatTensor([args.importance_weight_truncation])
        Qdiff = torch.zeros(1, action_size)
        Qrets[i + 1] = torch.zeros(1, action_size)
      else:
        importance_weight = log_probs[i + 1].data.exp() / 1  # TODO: Where does mu come from?
        Qdiff = Qrets[i + 1] - Qs[i + 1].data
      # ρ¯_i+1 = min(c, ρ_i+1)
      truncated_importance_weight = torch.clamp(importance_weight, min=args.importance_weight_truncation)
      # Qret(s_i, a_i) = r_i + γρ¯_i+1[Qret(s_i+1, s_t+1) − Q(s_t+1, s_t+1)] + γV(s_t+1)
      Qrets[i] = rewards[i] + args.discount * truncated_importance_weight.expand_as(Qdiff) * \
          Qdiff + args.discount * Vs[i + 1].data.expand_as(Qdiff)
      # dθ ← dθ - (Qret(s_i, a_i) - Q(s_i, a_i))∇θ∙Q(s_i, a_i)
      value_loss += (Variable(Qrets[i]) - Qs[i]).mean()  # TODO: Should operate only a of Q?

      # TD residual δ = r_i + γV(s_i+1; θ) - V(s_i; θ)
      td_error = rewards[i] + args.discount * Vs[i + 1].data - Vs[i].data
      # Generalised advantage estimator Ψ (roughly of form ∑(γλ)^t∙δ)
      A_GAE = A_GAE * args.discount * args.gae_discount + td_error
      # dθ ← dθ + ∇θ∙log(π(a_i|s_i; θ))∙Ψ - β∙∇θH(π(s_i; θ))
      policy_loss += -log_probs[i] * Variable(A_GAE) + args.entropy_weight * entropies[i]

    # TODO: Zero local grads too surely? When transferring, aren't shared lost anyway?
    optimiser.zero_grad()
    # Note that losses were defined as negatives of normal update rules for gradient descent
    (policy_loss + args.value_loss_weight * value_loss).backward(retain_variables=args.no_truncate)
    # Gradient (L2) norm clipping
    nn.utils.clip_grad_norm(model.parameters(), args.max_gradient_norm)

    # Transfer gradients to shared model and update
    _transfer_grads_to_shared_model(model, shared_model)
    optimiser.step()
    if args.lr_decay:
      # Decay learning rate
      _decay_learning_rate(optimiser, args.T_max)

  env.close()
