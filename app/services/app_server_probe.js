'use strict';

const kernel = Process.getModuleByName('kernelbase.dll');
const ntdll = Process.getModuleByName('ntdll.dll');
const native = (name, result, args) => new NativeFunction(kernel.getExportByName(name), result, args);
const getStdHandle = native('GetStdHandle', 'pointer', ['int']);
const getFileType = native('GetFileType', 'uint', ['pointer']);
const input = getStdHandle(-10);
const output = getStdHandle(-11);
if (getFileType(input) !== 3 || getFileType(output) !== 3) {
    throw new Error('目标标准输入输出不是管道');
}

function capture(direction, address, length) {
    // Binary transfer preserves UTF-8 characters split between native reads.
    const chunkSize = 65536;
    for (let offset = 0; offset < length; offset += chunkSize) {
        const size = Math.min(chunkSize, length - offset);
        send({kind: 'bytes', direction}, address.add(offset).readByteArray(size));
    }
}

Interceptor.attach(ntdll.getExportByName('NtReadFile'), {
    onEnter(args) {
        this.target = args[0].equals(input);
        if (!this.target) return;
        this.buffer = args[5];
        this.iosb = args[4];
    },
    onLeave(result) {
        if (!this.target || result.toInt32() !== 0) return;
        const length = this.iosb.add(Process.pointerSize).readU32();
        if (length === 0) return;
        capture('input', this.buffer, length);
    }
});
Interceptor.attach(ntdll.getExportByName('NtWriteFile'), {
    onEnter(args) {
        this.target = args[0].equals(output);
        if (this.target) {
            this.buffer = args[5];
            this.iosb = args[4];
            this.length = args[6].toUInt32();
        }
    },
    onLeave(result) {
        if (this.target && result.toInt32() === 0) {
            capture('output', this.buffer, Math.min(this.length,
                this.iosb.add(Process.pointerSize).readU32()));
        }
    }
});

rpc.exports = {
    ready() { return {pid: Process.id, arch: Process.arch}; },
    dispose() {
        Interceptor.detachAll();
    }
};
