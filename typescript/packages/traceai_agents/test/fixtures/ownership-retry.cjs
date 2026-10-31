// Report a real failed SQLite lock attempt so the parent can release its reader.
const Database = require("better-sqlite3");
const exec = Database.prototype.exec;
Database.prototype.exec = function (sql) {
  try { return exec.call(this, sql); }
  catch (error) {
    if (sql === "BEGIN EXCLUSIVE; COMMIT;" && error.code === "SQLITE_BUSY")
      process.send({ probe: "busy" });
    throw error;
  }
};
require("../../dist/src/runtime/storage-child.js");
