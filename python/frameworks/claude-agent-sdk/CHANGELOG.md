# Changelog

## 0.1.1

### Fixed
- Subagent tool calls nest under the call that launched them again on current Claude Agent SDK releases: the launch tool `Agent` is recognised alongside `Task`, and tool calls are attributed by `agent_id` through the `SubagentStart` hook, with `parent_tool_use_id` still honoured for older releases.
