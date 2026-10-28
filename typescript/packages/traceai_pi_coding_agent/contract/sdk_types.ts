// Compile-time proof that the real SDK types fit the structural types
// @traceai/pi-coding-agent accepts. `tsc --noEmit` fails if they drift.
import { createAgentSession, type AgentSession, type AgentSessionEvent } from "@earendil-works/pi-coding-agent";
import { PiCodingAgentInstrumentation, type PiSessionEvent, type PiSessionLike } from "@traceai/pi-coding-agent";

declare const session: AgentSession;
const asLike: PiSessionLike = session;
const instrumentation = new PiCodingAgentInstrumentation();
const traced: typeof createAgentSession = instrumentation.wrapCreateAgentSession(createAgentSession);
const stop: () => void = instrumentation.instrumentSession(session);
declare const event: AgentSessionEvent;
const asEvent: PiSessionEvent = event as PiSessionEvent;
void [asLike, traced, stop, asEvent];
