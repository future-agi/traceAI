// CLI trace probes use the shared Cursor hook route, not the separately held
// print-stream transport. Lifecycle/disable/uninstall still use cursor-cli.
module.exports = {
  ...require("./cursor.cjs").forAgent("cursor-cli"),
  captureAgent: "cursor",
};
