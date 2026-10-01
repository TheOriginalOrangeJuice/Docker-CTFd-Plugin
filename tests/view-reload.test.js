const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../assets/view.js'), 'utf8');

test('opening another Docker challenge reinitializes callbacks and clears old timers', () => {
    let nextTimer = 1;
    const timers = new Map();
    const cleared = [];
    const context = vm.createContext({
        CTFd: { _internal: { challenge: {}, markdown: { parse: value => value } } },
        document: { querySelector: () => null },
        setInterval(callback) {
            const id = nextTimer++;
            timers.set(id, callback);
            return id;
        },
        clearInterval(id) {
            cleared.push(id);
            timers.delete(id);
        },
    });

    vm.runInContext(source, context);
    assert.equal(timers.size, 1);
    const firstPoller = [...timers.keys()][0];
    vm.runInContext('revertCountdownTimer = setInterval(() => {}, 1000); expiryCountdownTimer = setInterval(() => {}, 1000);', context);
    const oldTimers = [...timers.keys()];
    assert.equal(oldTimers.length, 3);

    // CTFd resets its challenge callbacks before evaluating the next view script.
    context.CTFd._internal.challenge = {};
    assert.doesNotThrow(() => vm.runInContext(source, context));
    assert.equal(timers.size, 1, 'only the current challenge poller should remain');
    for (const timer of oldTimers) assert(cleared.includes(timer));
    assert(!timers.has(firstPoller));
    assert.equal(typeof context.CTFd._internal.challenge.preRender, 'function');
    assert.equal(typeof context.CTFd._internal.challenge.postRender, 'function');
    assert.equal(context.CTFd._internal.challenge.render('example'), 'example');
    assert.equal(typeof context.start_container, 'function', 'inline action handlers remain global');
    assert.equal(typeof context.stop_container, 'function');

    // Repeated navigation must not accumulate intervals, including a third open.
    context.CTFd._internal.challenge = {};
    assert.doesNotThrow(() => vm.runInContext(source, context));
    assert.equal(timers.size, 1);
    [...timers.values()][0](); // A closed challenge window stops the remaining poller.
    assert.equal(timers.size, 0);
});
