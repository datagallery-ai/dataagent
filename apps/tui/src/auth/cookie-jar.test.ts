import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { TuiCookieJar } from "./cookie-jar.js";

describe("TuiCookieJar", () => {
  it("parses multiple Set-Cookie headers and keeps only name/value", () => {
    const jar = new TuiCookieJar();
    const headers = new Headers();
    headers.append(
      "set-cookie",
      "df_session=abc%20123; Path=/; HttpOnly; Max-Age=3600",
    );
    headers.append("set-cookie", "df_csrf=token-value; Path=/; Max-Age=3600");
    jar.absorbSetCookie(headers);

    assert.deepEqual(jar.snapshot(), {
      df_session: "abc 123",
      df_csrf: "token-value",
    });
    assert.equal(jar.csrfToken(), "token-value");
    assert.equal(
      jar.headerValue(),
      "df_session=abc%20123; df_csrf=token-value",
    );
  });

  it("generates a stable Cookie header from snapshot order", () => {
    const jar = new TuiCookieJar();
    jar.replace({ df_session: "s1", df_csrf: "c1" });
    assert.equal(jar.headerValue(), "df_session=s1; df_csrf=c1");
  });

  it("replace clears previous cookies so sessions do not mix", () => {
    const jar = new TuiCookieJar();
    jar.replace({ df_session: "old", df_csrf: "old-csrf", leftover: "x" });
    jar.replace({ df_session: "new", df_csrf: "new-csrf" });
    assert.deepEqual(jar.snapshot(), {
      df_session: "new",
      df_csrf: "new-csrf",
    });
    assert.equal(jar.csrfToken(), "new-csrf");
  });

  it("does not expose toJSON to avoid accidental secret logging", () => {
    const jar = new TuiCookieJar();
    jar.replace({ df_session: "secret", df_csrf: "csrf" });
    assert.equal(
      Object.prototype.hasOwnProperty.call(jar, "toJSON"),
      false,
    );
    assert.equal(typeof (jar as { toJSON?: unknown }).toJSON, "undefined");
  });

  it("clear removes all cookies", () => {
    const jar = new TuiCookieJar();
    jar.replace({ df_session: "s", df_csrf: "c" });
    jar.clear();
    assert.deepEqual(jar.snapshot(), {});
    assert.equal(jar.headerValue(), undefined);
    assert.equal(jar.csrfToken(), undefined);
  });
});
