#!/usr/bin/env node
'use strict';
// Foundation bundle registers no host protocols. Unknown hosts receive no bytes,
// no permission decision and no disk/network work. A qualified adapter must own
// its neutral response contract before an installer may reference this entry.
process.stdin.resume();
const deadline = setTimeout(() => process.exit(0), 25);
process.stdin.on('end', () => { clearTimeout(deadline); process.exit(0); });
process.stdin.on('error', () => process.exit(0));
