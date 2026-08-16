import { cookies } from "next/headers";
import { COOKIE_NAME, decodeSession } from "./auth";

export async function currentSession() {
  return decodeSession((await cookies()).get(COOKIE_NAME)?.value);
}
