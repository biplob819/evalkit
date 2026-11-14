# C3.3 acceptance criteria

**SIM-01** Simulators run on Claude Haiku 5.5 and GPT Luna through the shared adapters with structured output; each reply is cached by (simulator, goal, intent, question history, trial, seed), so a rerun makes no calls and different trials are independent.
**SIM-02** The simulator prompt holds only the goal, the hidden intent and the agent's questions; a deterministic guard withholds any reply that states a gold value absent from the goal and intent; the 10 scripted checks pass on both simulators (live, cost reported) and offline with scripted adapters.
**SIM-03** Every trajectory records its simulator as `<model>@<prompt version>` (None on tasks without ask_user), and trials with different simulators never share a cache entry.
