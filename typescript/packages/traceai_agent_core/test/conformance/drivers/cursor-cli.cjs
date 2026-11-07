// Current CLI profile is held; trace probes exercise its documented shared IDE-hook
// route explicitly as cursor. CLI lifecycle/disable/uninstall still use cursor-cli.
module.exports = {
  ...require("./cursor.cjs").forAgent("cursor-cli"),
  captureAgent: "cursor",
};
