// Compiled from a consumer install with together-ai at the supported floor and latest release.
import Together from 'together-ai';
import { TogetherInstrumentation } from '@traceai/together';

const instrumentation = new TogetherInstrumentation();
instrumentation.manuallyInstrument(Together);

// Preserve namespace and CommonJS module attachment as public API as well.
import * as TogetherModule from 'together-ai';
instrumentation.manuallyInstrument(TogetherModule);
