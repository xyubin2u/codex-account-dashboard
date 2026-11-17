# Licenses and upstream components

This maintained distribution is licensed under GPL-3.0-only. The complete license is provided in `LICENSE`.

`vendor/codex-multi-account` includes the pinned codex-multi-account 0.8.10 runtime by rmqg, from <https://github.com/rmqg/codex-multi-account>, under GPL-3.0-only. Its original license and package attribution remain in that directory. `LOCAL.patch` and `patch_vendor*.py` record local changes, including process-bound session ownership, idle-task quota checks, quota reserve behavior and dynamic account labels.

The dashboard and Python adapters in this distribution are the maintained local integration. `agy_runtime` contains the required local AGY profile isolation/history and token refresh helpers; it does not contain the official AGY executable. OpenAI Codex CLI and the official AGY CLI are separate prerequisites with their own licenses and terms. No upstream authentication credentials or proprietary binaries are distributed.
