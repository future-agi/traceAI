// Synthetic transaction owner. Parent controls the lifetime, including SIGKILL.
const { paths, configTransaction } = require("../../dist/src/config.js");
configTransaction(paths(), async () => {
  process.send({ locked: true });
  await new Promise((resolve) => process.once("message", resolve));
}).then(
  () => {
    process.send({ released: true });
    process.disconnect();
  },
  (error) => {
    process.send({ held: error.code });
    process.disconnect();
  },
);
