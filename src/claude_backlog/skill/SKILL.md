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
4. **Write the prompt**, in the language of the conversation, following the
   user's prompt template: run `claude-backlog template` and keep its
   headings, replacing every line in angle brackets with real content (or
   deleting it where the template says a section is optional). With the
   default template that means:
   - **Goal**: what should be true when it is done, and why it matters;
   - **Context**: the findings from step 3, with concrete paths, the code
     to follow as a pattern, and the exact build, run and test commands;
   - **Constraints**: what must not change and what is out of scope;
   - **Done when**: results someone can observe, each with the exact
     command or step that checks it (prefer the project's own tests; ask
     for a test that fails before the change when that fits);
   - **Milestones**: for a long task, steps in order that can each be
     checked on their own;
   - **When unsure**: which choices Claude may make alone, which the user
     wants to make (they end up in the summary instead of being guessed),
     and what to do if something blocks the task.

   Use only paths and commands you have seen. Do not add rules about
   working unattended, STATE.md, SUMMARY.md, git branches, commits or not
   weakening tests: claude-backlog adds those itself. Keep the prompt
   focused on the task. If `claude-backlog template` prints no template,
   use the headings above.
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
