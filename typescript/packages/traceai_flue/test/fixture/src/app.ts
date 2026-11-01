import './telemetry.ts';
import { createAgentRouter } from '@flue/runtime/routing';
import { Hono } from 'hono';
import { Weather } from './agents/weather.ts';
const app = new Hono();
app.route('/agents/weather', createAgentRouter(Weather));
export default app;
