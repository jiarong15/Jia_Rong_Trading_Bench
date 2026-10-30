import {defineConfig} from '@playwright/test';
import path from 'node:path';
export default defineConfig({
 testDir:'./tests',fullyParallel:false,workers:1,timeout:60000,
 use:{baseURL:'http://127.0.0.1:8001',headless:true,viewport:{width:1440,height:1050},channel:process.platform==='win32'?'msedge':undefined,screenshot:'only-on-failure'},
 webServer:{command:'python -m uvicorn backend.app:app --host 127.0.0.1 --port 8001',url:'http://127.0.0.1:8001/api/health',reuseExistingServer:false,timeout:180000,env:{...process.env,WORKBENCH_DB:path.resolve(`../.runtime/browser-tests-${Date.now()}.sqlite3`)}},
});
