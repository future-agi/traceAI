/*
 * Copyright Future AGI
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *      https://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */
import type * as llamaindex from "llamaindex";

import {
  InstrumentationBase,
  InstrumentationModuleDefinition,
  InstrumentationNodeModuleDefinition,
  isWrapped,
} from "@opentelemetry/instrumentation";

import { LlamaIndexInstrumentationConfig } from "./types";
import { chatWrapper, genericWrapper, Method, MethodWrapper } from "./wrapper";
import { isLLM } from "./utils";

import {
  FISpanKind,
} from "@traceai/fi-semantic-conventions";
import { VERSION } from "./version";

interface PatchRule {
  method: string;
  kind: FISpanKind;
  appliesTo: (exportName: string, prototype: object) => boolean;
}

type MethodTable = Record<string, Method>;

interface PatchTarget {
  prototype: MethodTable;
  className: string;
  rule: PatchRule;
}

const ownsMethod = (prototype: object, method: string) =>
  Object.prototype.hasOwnProperty.call(prototype, method);

const definesMethod = (method: string) => (_: string, prototype: object) =>
  ownsMethod(prototype, method);

const namedClass = (name: string) => (exportName: string) =>
  exportName === name;

const PATCH_RULES: PatchRule[] = [
  {
    method: "chat",
    kind: FISpanKind.LLM,
    appliesTo: (_, prototype) => isLLM(prototype) && ownsMethod(prototype, "chat"),
  },
  {
    method: "getQueryEmbedding",
    kind: FISpanKind.EMBEDDING,
    appliesTo: definesMethod("getQueryEmbedding"),
  },
  { method: "synthesize", kind: FISpanKind.CHAIN, appliesTo: definesMethod("synthesize") },
  { method: "retrieve", kind: FISpanKind.RETRIEVER, appliesTo: definesMethod("retrieve") },
  { method: "query", kind: FISpanKind.CHAIN, appliesTo: namedClass("RetrieverQueryEngine") },
  { method: "chat", kind: FISpanKind.CHAIN, appliesTo: namedClass("ContextChatEngine") },
  { method: "chat", kind: FISpanKind.AGENT, appliesTo: namedClass("OpenAIAgent") },
];

export class LlamaIndexInstrumentation extends InstrumentationBase {
  declare protected _config: LlamaIndexInstrumentationConfig;

  constructor(config: LlamaIndexInstrumentationConfig = {}) {
    super("@traceai/llamaindex", VERSION, config);
  }

  public override setConfig(config: LlamaIndexInstrumentationConfig = {}) {
    super.setConfig(config);
  }

  /**
   * Instruments `llamaindex` plus any provider packages whose classes it no
   * longer re-exports, e.g. `manuallyInstrument(LlamaIndex, LlamaIndexOpenAI)`.
   */
  public manuallyInstrument(
    module: typeof llamaindex,
    ...providerModules: object[]
  ) {
    this._diag.debug("Manually instrumenting llamaindex");

    const modules = [module, ...providerModules];
    modules.forEach((moduleExports) => this.patch(moduleExports));

    const hasLLM = modules.some((moduleExports) =>
      this.patchTargets(moduleExports).some(
        ({ rule }) => rule.kind === FISpanKind.LLM,
      ),
    );
    if (!hasLLM) {
      this._diag.warn(
        "No LLM classes found; pass provider packages too, e.g. manuallyInstrument(LlamaIndex, LlamaIndexOpenAI)",
      );
    }
  }

  protected init(): InstrumentationModuleDefinition[] {
    return ["llamaindex", "@llamaindex/openai"].map(
      (name) =>
        new InstrumentationNodeModuleDefinition(
          name,
          [">=0.1.0"],
          this.patch.bind(this),
          this.unpatch.bind(this),
        ),
    );
  }

  private wrapperFor({ rule, className }: PatchTarget): MethodWrapper {
    return rule.kind === FISpanKind.LLM
      ? chatWrapper({ className }, this._config, this._diag, () => this.tracer)
      : genericWrapper(className, rule.method, rule.kind, () => this.tracer);
  }

  private patchTargets(moduleExports: object): PatchTarget[] {
    const targets: PatchTarget[] = [];

    for (const [exportName, value] of Object.entries(moduleExports)) {
      const prototype: unknown =
        typeof value === "function" ? value.prototype : undefined;
      if (!prototype || typeof prototype !== "object") {
        continue;
      }
      for (const rule of PATCH_RULES) {
        if (
          typeof (prototype as MethodTable)[rule.method] === "function" &&
          rule.appliesTo(exportName, prototype)
        ) {
          targets.push({
            prototype: prototype as MethodTable,
            className: value.name || exportName,
            rule,
          });
        }
      }
    }

    return targets;
  }

  private patch(moduleExports: object, moduleVersion?: string) {
    this._diag.debug(`Patching llamaindex module@${moduleVersion}`);

    try {
      const targets = this.patchTargets(moduleExports);
      if (targets.length === 0) {
        this._diag.warn(
          "No LlamaIndex classes found to instrument in the given module",
        );
      }
      for (const target of targets) {
        const { prototype, rule } = target;
        if (!isWrapped(prototype[rule.method])) {
          this._wrap(prototype, rule.method, this.wrapperFor(target));
        }
      }
    } catch (error) {
      this._diag.error("Failed to instrument LlamaIndex module", error);
    }

    return moduleExports;
  }

  private unpatch(moduleExports: object, moduleVersion?: string) {
    this._diag.debug(`Unpatching llamaindex module@${moduleVersion}`);

    for (const { prototype, rule } of this.patchTargets(moduleExports)) {
      if (isWrapped(prototype[rule.method])) {
        this._unwrap(prototype, rule.method);
      }
    }

    return moduleExports;
  }
}
