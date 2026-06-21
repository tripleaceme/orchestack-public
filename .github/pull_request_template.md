Resolves #

<!---
  Reference the issue this PR resolves, if there is one. PRs that change
  behaviour without a linked issue may be asked to open one first so the
  intent can be discussed before review effort is spent.
-->

### Problem

<!---
  What problem does this PR solve? Describe the state of the platform
  BEFORE this PR — what was broken, missing, or unclear.
-->

### Solution

<!---
  How does this PR solve the problem? Walk a reviewer through your
  approach. Name any alternatives you considered and why you chose this
  one. Call out anything you'd like a second opinion on.
-->

### Verification

<!---
  How did you verify the change works? Concrete steps a reviewer can
  follow to reproduce your test. Reference the relevant smoke procedure
  from docs/services/<service>.html if you exercised one.
-->

### Checklist

- [ ] I have read [CONTRIBUTING.md](../CONTRIBUTING.md) and understand the
      review and release process.
- [ ] I have run the relevant smoke procedure for the area I changed and
      it passes.
- [ ] If this PR changes operator-facing behaviour, I have updated the
      relevant page under `docs/` (edit the Python source in
      `_generate_docs.py`, not the generated HTML).
- [ ] If this PR adds, renames, or removes an environment variable, I
      have updated `system/docker/.env.example` and noted it in the PR
      description.
- [ ] My commits are signed-off (`git commit -s`) and have descriptive
      messages following the convention in CONTRIBUTING.md.
