/** Tokenize Node's environment options without rewriting their original spelling. */
export function assertNoPreload(options = ''): void {
  const tokens: string[] = [];
  let token = '', quoted = false;
  for (let i = 0; i < options.length; i++) {
    const char = options[i];
    if (char === '\\' && quoted) {
      if (++i === options.length) throw new Error('OPENCLAW_INVALID_NODE_OPTIONS');
      token += options[i];
    } else if (char === '"') quoted = !quoted;
    else if (/\s/.test(char) && !quoted) {
      if (token) tokens.push(token);
      token = '';
    } else token += char;
  }
  if (quoted) throw new Error('OPENCLAW_INVALID_NODE_OPTIONS');
  if (token) tokens.push(token);
  if (tokens.some((part) => /^--(?:import|require|loader|experimental[-_]loader)(?:=|$)/.test(part) || /^-r/.test(part)))
    throw new Error('OPENCLAW_EXISTING_PRELOAD');
}

export function withBootstrap(options: string | undefined, bootstrapUrl: string): string {
  assertNoPreload(options);
  return `${options ? options + ' ' : ''}--import=${bootstrapUrl}`;
}

/** Restore only the options the launcher retained, not its own bootstrap. */
export function restoreNodeOptions(env: NodeJS.ProcessEnv): void {
  const original = env.FI_OPENCLAW_NODE_OPTIONS;
  if (original) env.NODE_OPTIONS = original;
  else delete env.NODE_OPTIONS;
  delete env.FI_OPENCLAW_NODE_OPTIONS;
}
