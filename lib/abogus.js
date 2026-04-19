#!/usr/bin/env node
/*
 * Wrapper CLI for Douyin a_bogus signing.
 *
 * Usage:
 *   node abogus.js <query_string> <user_agent>
 *
 * Input:
 *   argv[2]  raw URL query string, e.g.  aid=6383&device_platform=webapp&...
 *   argv[3]  full browser User-Agent string
 *
 * Output (stdout, single line):
 *   <original_qs>&a_bogus=<sig>
 */

const fs   = require('fs');
const path = require('path');

const core = fs.readFileSync(path.join(__dirname, 'a_bogus_core.js'), 'utf8');
// eslint-disable-next-line no-eval
eval(core);

const qs = process.argv[2] || '';
const ua = process.argv[3] || '';

if (!qs || !ua) {
    console.error('Usage: node abogus.js <query_string> <user_agent>');
    process.exit(2);
}

try {
    const sig = generate_a_bogus(qs, ua);
    const joined = (qs.length ? qs + '&' : '') + 'a_bogus=' + encodeURIComponent(sig);
    process.stdout.write(joined);
} catch (e) {
    console.error('Signing failed:', e && e.stack || e);
    process.exit(1);
}
