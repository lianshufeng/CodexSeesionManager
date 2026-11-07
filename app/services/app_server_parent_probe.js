'use strict';

const kernel = Process.getModuleByName('kernelbase.dll');
const getFileType = new NativeFunction(kernel.getExportByName('GetFileType'), 'uint', ['pointer']);
const hook = Interceptor.attach(Process.getModuleByName('ntdll.dll').getExportByName('NtWriteFile'), {
    onEnter(args) {
        const size = args[6].toUInt32();
        if (size > 0 && size <= 4194304 && getFileType(args[0]) === 3) {
            send({kind: 'parentBytes', handle: args[0].toString()}, args[5].readByteArray(size));
        }
    }
});
rpc.exports = {dispose() { hook.detach(); }};
