import * as transformers from '@huggingface/transformers';
import { TransformersInstrumentation } from '@traceai/huggingface-transformers';

const instrumentation = new TransformersInstrumentation();
const wrapped: typeof transformers = instrumentation.wrapModule(transformers);
const generation = await wrapped.pipeline('text-generation', 'unused-at-typecheck');
const sameGeneration: typeof generation = instrumentation.wrapPipeline(generation);
await sameGeneration('hello', { max_new_tokens: 2 });
await sameGeneration([{ role: 'user', content: 'hello' }], { max_new_tokens: 2 });
const qa = await wrapped.pipeline('question-answering', 'unused-at-typecheck');
await qa('who?', 'Alice lives here.');
const features = await wrapped.pipeline('feature-extraction', 'unused-at-typecheck');
const tensor: transformers.Tensor = await features(['one', 'two'], { pooling: 'mean', normalize: true });
const sameFeatures: typeof features = instrumentation.wrapPipeline(features);
await sameFeatures('hello');
await sameGeneration.dispose();
void tensor;
