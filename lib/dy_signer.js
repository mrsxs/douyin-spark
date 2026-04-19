#!/usr/bin/env node
/*
 * CLI wrapper for Douyin IM signing (dy_ab.js functions).
 *
 * Usage:
 *   node dy_signer.js req_sign '<json_of_e>' '<private_key_pem>'
 *   node dy_signer.js a_bogus  '<params>' '<data>'
 *   node dy_signer.js ree_key  '<private_key_pem>'
 */

const fs   = require('fs');
const path = require('path');

// jsrsasign 是 dy_ab.js 唯一的外部依赖
global.jsrsasign = require('jsrsasign');

const core = fs.readFileSync(path.join(__dirname, 'dy_ab.js'), 'utf8');
// eslint-disable-next-line no-eval
eval(core);

const cmd = process.argv[2];
try {
    let out = '';
    if (cmd === 'req_sign') {
        const e    = JSON.parse(process.argv[3]);
        const prik = process.argv[4];
        out = get_req_sign(e, prik);
    } else if (cmd === 'a_bogus') {
        const params = process.argv[3] || '';
        const data   = process.argv[4] || '';
        out = get_ab(params, data);
    } else if (cmd === 'ree_key') {
        const prik = process.argv[3];
        out = get_ree_key(prik);
    } else {
        console.error('usage: dy_signer.js <req_sign|a_bogus|ree_key> ...');
        process.exit(2);
    }
    process.stdout.write(String(out));
} catch (e) {
    console.error('signer failed:', e && e.stack || e);
    process.exit(1);
}
