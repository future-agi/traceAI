const { mkdtempSync, rmSync } = require("node:fs");
const { tmpdir } = require("node:os");
const { basename, dirname, join } = require("node:path");
const { createRequire } = require("node:module");

const testHome = mkdtempSync(join(tmpdir(), "traceai-n8n-"));
process.env.N8N_USER_FOLDER = testHome;
process.env.N8N_ENCRYPTION_KEY = "traceai-n8n-tests-not-a-real-key";
process.env.N8N_DIAGNOSTICS_ENABLED = "false";

const { Workflow, Expression } = require("n8n-workflow");
const { WorkflowExecute, ExecutionLifecycleHooks } = require("n8n-core");
const coreRequire = createRequire(require.resolve("n8n-core"));
const { Container } = coreRequire("@n8n/di");
const { SSHClientsManager } = coreRequire(
  "./execution-engine/ssh-clients-manager.js",
);

function fixture({
  id = "workflow-1",
  execute,
  afterTransform = false,
  Engine = WorkflowExecute,
} = {}) {
  const nodes = [
    {
      id: "start-id",
      name: "Start",
      type: "traceai.testStart",
      typeVersion: 1,
      position: [0, 0],
      parameters: {},
    },
    {
      id: "transform-id",
      name: "Transform",
      type: "traceai.testTransform",
      typeVersion: 1,
      position: [250, 0],
      parameters: {},
    },
  ];
  if (afterTransform) {
    nodes.push({
      id: "finish-id",
      name: "Finish",
      type: "traceai.testFinish",
      typeVersion: 1,
      position: [500, 0],
      parameters: {},
    });
  }
  const description = {
    displayName: "Test node",
    name: "test",
    group: ["transform"],
    version: 1,
    description: "A local node executed by the real n8n engine",
    defaults: { name: "Test" },
    inputs: ["main"],
    outputs: ["main"],
    properties: [],
  };
  const types = {
    "traceai.testStart": {
      description: { ...description, inputs: [] },
      async execute() {
        return [[{ json: { value: 21, payload: "not-for-tracing" } }]];
      },
    },
    "traceai.testTransform": {
      description,
      execute:
        execute ??
        async function () {
          return [
            this.getInputData().map((item) => ({
              json: { ...item.json, value: item.json.value * 2 },
            })),
          ];
        },
    },
    "traceai.testFinish": {
      description,
      async execute() {
        return [this.getInputData()];
      },
    },
  };
  const nodeTypes = {
    getByName: (name) => types[name],
    getByNameAndVersion: (name) => types[name],
    getKnownTypes: () => ({}),
  };
  const workflow = new Workflow({
    id,
    name: "Local transformation",
    nodes,
    connections: {
      Start: { main: [[{ node: "Transform", type: "main", index: 0 }]] },
      ...(afterTransform
        ? {
            Transform: { main: [[{ node: "Finish", type: "main", index: 0 }]] },
          }
        : {}),
    },
    active: false,
    nodeTypes,
    settings: { executionOrder: "v1", timezone: "UTC" },
  });
  const hooks = new ExecutionLifecycleHooks("manual", `execution-${id}`, {
    id,
    name: workflow.name,
    nodes,
    connections: workflow.connectionsBySourceNode,
    active: false,
    settings: workflow.settings,
  });
  const engine = new Engine(
    {
      hooks,
      executionId: `execution-${id}`,
      currentNodeExecutionIndex: 0,
      variables: {},
      logAiEvent() {},
      restApiUrl: "http://unused.invalid/",
      instanceBaseUrl: "http://unused.invalid/",
    },
    "manual",
  );
  return {
    engine,
    workflow,
    hooks,
    run: () => engine.run({ workflow, startNode: nodes[0] }),
  };
}

async function cleanup() {
  // The real engine owns a recurring SSH cleanup timer and expression resources.
  Container.get(SSHClientsManager).onShutdown();
  await Expression.disposeExpressionEngine();
  if (
    dirname(testHome) === tmpdir() &&
    basename(testHome).startsWith("traceai-n8n-")
  ) {
    rmSync(testHome, { recursive: true, force: true });
  }
}

module.exports = { fixture, cleanup, WorkflowExecute };
