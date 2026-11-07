export { createAutoSpanDelivery, rememberAutoModelCoverage, hasAutoModelCoverage } from "./auto-span-delivery";
export { createAutoModelOperation, getAutoModelOperation, rememberAutoModelOperation,
  activeAutoModelOperation, withAutoModelOperation, autoModelResponsibility, hasAutoSpanSink, rememberAutoSpanSink, autoModelWireAllowed } from "./auto-span-delivery";
export type { AutoSpanDelivery, AutoModelOperation } from "./auto-span-delivery";
export * from "./trace";
export * from "./utils";
export type * from "./utils/types";
export * from "./otel";
export * from "./fi_types";
export { registerSecret, getRegisteredSecrets, seedSecrets, markSecretDiscoveryFailed } from "./secrets";
export { privacyFilter, LIBRARY_SECRETS, rememberMediaAdmission, rememberSpanValueLimit } from "./export-privacy";
export type { PrivacyOptions } from "./export-privacy";
export { projectAttributes, tracerAttributeLimit, DROPPED_CONTENT_COUNT } from "./attribute-projection";
export * from "./awsLambda";

export { rememberProviderResponse } from "./privacy-runtime";
