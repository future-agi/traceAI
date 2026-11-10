/** All recognition is core-owned; transports and export snapshots use the same S6 predicates. */
export { MAX_DISCOVERY_BODY_BYTES, CREDENTIAL_HEADERS, normalizePrivacyName, isCredentialName, mediaReplacement, indexPrivacyHints, scrubText, scrubValue, scrubAttribute, discoverSecrets, credentialSecrets, scrubProviderResponse, discoverProviderResponseSecrets } from "@traceai/fi-core/privacy";
