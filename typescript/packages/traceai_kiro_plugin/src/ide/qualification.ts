/** No environment variable, repository file or caller flag can qualify an IDE build.
 * Fill only after a source-bound real GUI capture verifies these exact contracts:
 * v1 registration, permission consent, session/turn payloads, IDE-vs-CLI source
 * identity, terminal outcomes, disable, exporter outage and empty hook output.
 */
export const QUALIFIED_IDE_VERSIONS: readonly string[] = Object.freeze([]);
export const IDE_QUALIFIED = QUALIFIED_IDE_VERSIONS.length > 0;
