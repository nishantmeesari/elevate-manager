import { describe, expect, it } from "vitest";
import { viewFromPath } from "./admin";

describe("admin routing", () => {
  it("defaults unknown admin paths to overview", () => {
    window.history.replaceState({}, "", "/admin/unknown");
    expect(viewFromPath()).toBe("overview");
  });

  it("maps supported admin paths", () => {
    window.history.replaceState({}, "", "/admin/customers");
    expect(viewFromPath()).toBe("customers");

    window.history.replaceState({}, "", "/admin/payments");
    expect(viewFromPath()).toBe("payments");
  });

  it("keeps the customer root independent", () => {
    window.history.replaceState({}, "", "/");
    expect(viewFromPath()).toBe("overview");
  });
});
