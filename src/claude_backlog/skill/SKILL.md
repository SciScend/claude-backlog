---
name: backlog
description: Put a task into the claude-backlog queue so Claude Code does it later, unattended, while the user is away. Use when the user types /backlog, or asks to add a task to the backlog, queue it for later, or let Claude do it overnight or while they are out. Writes a self-contained prompt from the conversation and the project, shows it, and adds it with `claude-backlog add` after the user's OK.
---

<!-- installed by claude-backlog; 'claude-backlog install' keeps it up to date -->

# Add a task to the backlog

The task will run later with nobody at the keyboard: no questions can be
asked and no one will approve anything. The prompt you write is all the
next Claude session will know, so it must stand on its own.

## Steps

1. **Find the task.** Use the arguments of `/backlog` if there are any,
   otherwise the job that was just discussed. If it is really unclear what
   the task is, ask once; otherwise do not ask.
2. **Find the project folder.** Default: the git root of the current
   working directory. Use another folder only if the task is clearly about
   it.
3. **Collect what the next session needs**, by reading the code (read-only):
   the files and folders involved, how the project is built and tested (the
   exact commands), conventions that matter, and anything decided in this
   conversation that is not written down anywhere.
4. **Write the prompt**, in the language of the conversation. Include:
   - the goal, in one or two sentences, and why it matters;
   - the context from step 3, with concrete paths;
   - the scope: what to change and what to leave alone;
   - the steps, if the order matters;
   - how to check the result (commands to run, what must pass);
   - when the task counts as done;
   - the decisions the user wants to make themselves, so they are listed in
     the summary rather than guessed.

   Do not add rules about working unattended, STATE.md, SUMMARY.md, git
   branches or commits: claude-backlog adds those itself. Keep the prompt
   focused on the task.
5. **Propose it.** Show the title (short, imperative), the priority (high,
   normal or low; normal unless the user said otherwise), the project folder,
   and the full prompt. Mention a model or effort only if the task needs one
   that differs from the defaults. Ask for an OK; apply any changes the user
   asks for.
6. **Add it** after the OK:

   ```bash
   claude-backlog add -C "<project folder>" -t "<title>" -p <priority> - <<'PROMPT'
   <the prompt>
   PROMPT
   ```

   Add `--hold` if the user wants it on hold rather than in the queue.
7. **Confirm** with the task id that `claude-backlog add` printed, and
   mention that the board (`claude-backlog open`) shows the queue. The task
   runs when the user presses Run now, or when they are away if that is
   switched on.

If `claude-backlog` is not installed, say so and show the prompt anyway, so
it can be added by hand.
