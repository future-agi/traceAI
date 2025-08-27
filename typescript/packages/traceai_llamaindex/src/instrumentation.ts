/*
 * Copyright Traceloop
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
} from "@opentelemetry/instrumentation";

import { LlamaIndexInstrumentationConfig } from "./types";
import { chatWrapper, genericWrapper } from "./wrapper";
import {
  shouldSendPrompts,
  isLLM,
  isEmbedding,
  isSynthesizer,
  isRetriever,
} from "./utils";

import {
  SemanticConventions,
  LLMSystem,
  FISpanKind,
  LLMProvider,
} from "@traceai/fi-semantic-conventions";
import { VERSION } from "./version";

export class LlamaIndexInstrumentation extends InstrumentationBase {
  declare protected _config: LlamaIndexInstrumentationConfig;

  constructor(config: LlamaIndexInstrumentationConfig = {}) {
    super("@traceai/instrumentation-llamaindex", VERSION, config);
  }

  public override setConfig(config: LlamaIndexInstrumentationConfig = {}) {
    super.setConfig(config);
  }

  public manuallyInstrument(module: typeof llamaindex) {
    this._diag.debug("Manually instrumenting llamaindex");

    this.patch(module);
  }

  protected init(): InstrumentationModuleDefinition[] {
    const llamaindexModule = new InstrumentationNodeModuleDefinition(
      "llamaindex",
      [">=0.1.0"],
      this.patch.bind(this),
      this.unpatch.bind(this),
    );

    const openaiModule = new InstrumentationNodeModuleDefinition(
      "@llamaindex/openai",
      [">=0.1.0"],
      this.patchOpenAI.bind(this),
      this.unpatchOpenAI.bind(this),
    );

    return [llamaindexModule, openaiModule];
  }

  private patch(moduleExports: typeof llamaindex, moduleVersion?: string) {
    this._diag.debug(`Patching llamaindex@${moduleVersion}`);

    this._wrap(
      moduleExports.RetrieverQueryEngine.prototype,
      "query",
      genericWrapper(
        moduleExports.RetrieverQueryEngine.name,
        "query",
        FISpanKind.CHAIN,
        () => this.tracer,
      ),
    );

    this._wrap(
      moduleExports.ContextChatEngine.prototype,
      "chat",
      genericWrapper(
        moduleExports.ContextChatEngine.name,
        "chat",
        FISpanKind.CHAIN,
        () => this.tracer,
      ),
    );

    // OpenAIAgent has been moved to @llamaindex/openai package in newer versions
    // This instrumentation is handled separately

    for (const key in moduleExports) {
      const cls = (moduleExports as any)[key];
      if (isLLM(cls.prototype)) {
        this._wrap(
          cls.prototype,
          "chat",
          chatWrapper(
            { className: cls.name },
            this._config,
            this._diag,
            () => this.tracer,
          ),
        );
      } else if (isEmbedding(cls.prototype)) {
        this._wrap(
          cls.prototype,
          "getQueryEmbedding",
          genericWrapper(
            cls.name,
            "getQueryEmbedding",
            FISpanKind.EMBEDDING,
            () => this.tracer,
          ),
        );
      } else if (isSynthesizer(cls.prototype)) {
        this._wrap(
          cls.prototype,
          "synthesize",
          genericWrapper(
            cls.name,
            "synthesize",
            FISpanKind.CHAIN,
            () => this.tracer,
          ),
        );
      } else if (isRetriever(cls.prototype)) {
        this._wrap(
          cls.prototype,
          "retrieve",
          genericWrapper(
            cls.name,
            "retrieve",
            FISpanKind.CHAIN,
            () => this.tracer,
          ),
        );
      }
    }

    return moduleExports;
  }

  private unpatch(moduleExports: typeof llamaindex, moduleVersion?: string) {
    this._diag.debug(`Unpatching llamaindex@${moduleVersion}`);

    this._unwrap(moduleExports.RetrieverQueryEngine.prototype, "query");

    for (const key in moduleExports) {
      const cls = (moduleExports as any)[key];
      if (isLLM(cls.prototype)) {
        this._unwrap(cls.prototype, "complete");
        this._unwrap(cls.prototype, "chat");
      } else if (isEmbedding(cls.prototype)) {
        this._unwrap(cls.prototype, "getQueryEmbedding");
      } else if (isSynthesizer(cls.prototype)) {
        this._unwrap(cls.prototype, "synthesize");
      } else if (isRetriever(cls.prototype)) {
        this._unwrap(cls.prototype, "retrieve");
      }
    }

    return moduleExports;
  }

  private patchOpenAI(moduleExports: any, moduleVersion?: string) {
    this._diag.debug(`Patching @llamaindex/openai@${moduleVersion}`);

    // Instrument OpenAIAgent if it exists
    if (moduleExports.OpenAIAgent && moduleExports.OpenAIAgent.prototype) {
      this._wrap(
        moduleExports.OpenAIAgent.prototype,
        "chat",
        genericWrapper(
          moduleExports.OpenAIAgent.name,
          "agent",
          FISpanKind.AGENT,
          () => this.tracer,
        ),
      );
    }

    return moduleExports;
  }

  private unpatchOpenAI(moduleExports: any, moduleVersion?: string) {
    this._diag.debug(`Unpatching @llamaindex/openai@${moduleVersion}`);

    // Unwrap OpenAIAgent if it exists
    if (moduleExports.OpenAIAgent && moduleExports.OpenAIAgent.prototype) {
      this._unwrap(moduleExports.OpenAIAgent.prototype, "chat");
    }

    return moduleExports;
  }
}
