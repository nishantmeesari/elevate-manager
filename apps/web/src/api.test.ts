import { beforeEach, describe, expect, it, vi } from "vitest";
import { api, getStaffRole, getToken, setStaffToken, setToken } from "./api";

describe("customer API client", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.stubGlobal("fetch", vi.fn());
  });

  it("sends login credentials to the real customer auth endpoint", async () => {
    vi.mocked(fetch).mockResolvedValue(
      new Response(JSON.stringify({ access_token: "member-token" }), { status: 200 }),
    );

    const response = await api.login({
      email: "member@example.test",
      password: "A secure password",
    });

    expect(response.access_token).toBe("member-token");
    expect(fetch).toHaveBeenCalledWith(
      "/api/auth/login",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          email: "member@example.test",
          password: "A secure password",
        }),
      }),
    );
  });

  it("attaches the stored token to booking mutations", async () => {
    setToken("member-token");
    vi.mocked(fetch).mockResolvedValue(
      new Response(JSON.stringify({ id: "booking-1" }), { status: 201 }),
    );

    await api.createBooking({
      session_id: "session-1",
      customer_id: "customer-1",
      member_pass_id: "pass-1",
      idempotency_key: "booking-key",
    });

    const [, options] = vi.mocked(fetch).mock.calls[0];
    expect(options?.headers).toBeInstanceOf(Headers);
    expect((options?.headers as Headers).get("Authorization")).toBe(
      "Bearer member-token",
    );
    expect((options?.headers as Headers).get("Content-Type")).toBe(
      "application/json",
    );
  });

  it("clears credentials when a protected endpoint returns unauthorized", async () => {
    setToken("expired-token");
    vi.mocked(fetch).mockResolvedValue(
      new Response(JSON.stringify({ detail: "Account is inactive or unavailable" }), {
        status: 401,
      }),
    );

    await expect(api.profile()).rejects.toMatchObject({
      status: 401,
      detail: "Account is inactive or unavailable",
    });
    expect(getToken()).toBeNull();
  });

  it("uses the staff login endpoint and attaches a staff token", async () => {
    vi.mocked(fetch)
      .mockResolvedValueOnce(new Response(JSON.stringify({ access_token: "staff-token" }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ customers: 4 }), { status: 200 }));

    await api.staffLogin({
      email: "owner@example.test",
      password: "A secure password",
    });
    setStaffToken("staff-token");

    await api.admin.dashboard();

    expect(fetch).toHaveBeenNthCalledWith(
      1,
      "/api/auth/staff/login",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          email: "owner@example.test",
          password: "A secure password",
        }),
      }),
    );
    const [, dashboardOptions] = vi.mocked(fetch).mock.calls[1];
    expect((dashboardOptions?.headers as Headers).get("Authorization")).toBe(
      "Bearer staff-token",
    );
  });

  it("decodes owner and receptionist staff roles from their tokens", () => {
    const encodePayload = (role: "owner" | "receptionist") =>
      btoa(JSON.stringify({ typ: "user", role }))
        .replace(/\+/g, "-")
        .replace(/\//g, "_")
        .replace(/=+$/, "");

    setStaffToken(`${encodePayload("owner")}.signature`);
    expect(getStaffRole()).toBe("owner");
    setStaffToken(`${encodePayload("receptionist")}.signature`);
    expect(getStaffRole()).toBe("receptionist");
  });
});
