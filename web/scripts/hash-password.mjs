import { randomBytes, scryptSync } from "node:crypto";
const password = process.argv[2];
if (!password) { console.error("usage: npm run hash-password -- 'strong password'"); process.exit(2); }
const salt = randomBytes(16).toString("hex");
console.log(JSON.stringify({ salt, password_hash: scryptSync(password, Buffer.from(salt, "hex"), 64).toString("hex") }, null, 2));
