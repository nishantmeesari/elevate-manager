export type Service = {
  id: string;
  name: string;
  description: string | null;
  duration_minutes: number;
  default_capacity: number;
  price_paise: number;
};

export type Trainer = {
  id: string;
  full_name: string;
  services: string[];
};

export type Session = {
  id: string;
  service_id: string;
  service_name: string;
  trainer_id: string;
  trainer_name: string;
  starts_at: string;
  ends_at: string;
  capacity: number;
  occupied_seats: number;
  available_seats: number;
};

export type Booking = {
  id: string;
  booking_ref: string;
  session_id: string;
  customer_id: string;
  status: string;
  payment_mode: string;
  member_pass_id: string;
  price_paise: number;
  hold_expires_at: string | null;
  reschedule_count: number;
  starts_at?: string;
  ends_at?: string;
  service_name?: string;
  trainer_name?: string;
};

export type PassBalance = {
  id: string;
  pass_plan_id: string;
  name: string;
  sessions_total: number;
  purchased_at: string;
  expires_at: string;
  status: string;
  credits_remaining: number;
};

export type Payment = {
  id: string;
  invoice_id: string;
  kind: string;
  amount_paise: number;
  method: string;
  reference: string | null;
  received_at: string;
};

export type Invoice = {
  invoice_id: string;
  invoice_number: string;
  total_paise: number;
  paid_paise: number;
  refunded_paise: number;
  balance_paise: number;
  payment_status: string;
};

export type Profile = {
  id: string;
  full_name: string;
  phone_e164: string;
  email: string | null;
  whatsapp_opt_in: boolean;
  created_at: string;
};

export type StaffRole = "owner" | "receptionist" | "trainer";

export type AdminDashboard = {
  customers: number;
  confirmed_bookings: number;
  pending_payments: number;
  active_passes: number;
  active_waitlist: number;
};

export type AdminCustomer = {
  id: string;
  full_name: string;
  phone_e164: string;
  email: string | null;
  whatsapp_opt_in: boolean;
  created_at: string;
};

export type AdminService = {
  id: string;
  name: string;
  description: string | null;
  duration_minutes: number;
  default_capacity: number;
  price_paise: number;
  tax_rate_bps: number;
  is_active: boolean;
};

export type AdminTrainer = {
  id: string;
  full_name: string;
  phone_e164: string | null;
  is_active: boolean;
};

export type PassPlan = {
  id: string;
  name: string;
  session_count: number;
  validity_days: number;
  price_paise: number;
  tax_rate_bps: number;
  is_active: boolean;
};

export type AdminPass = {
  id: string;
  customer_id: string;
  pass_plan_id: string;
  customer_name: string;
  plan_name: string;
  sessions_total: number;
  purchased_at: string;
  expires_at: string;
  status: string;
  credits_remaining: number;
};

export type AdminBooking = {
  id: string;
  booking_ref: string;
  session_id: string;
  customer_id: string;
  customer_name: string;
  service_name: string;
  starts_at: string;
  status: string;
  payment_mode: string;
  price_paise: number;
  member_pass_id: string | null;
  reschedule_count: number;
};

export type AdminPayment = {
  id: string;
  invoice_id: string;
  invoice_number: string;
  customer_name: string;
  kind: string;
  amount_paise: number;
  method: string;
  reference: string | null;
  received_at: string;
};

export type AdminInvoice = {
  invoice_id: string;
  customer_name: string;
  invoice_number: string;
  total_paise: number;
  paid_paise: number;
  refunded_paise: number;
  balance_paise: number;
  payment_status: string;
  issue_date: string;
};

export type BusinessSetting = {
  key: string;
  value: unknown;
  updated_at: string;
};

export type WaitlistEntry = {
  id: string;
  session_id: string;
  customer_id: string;
  customer_name: string;
  status: string;
  offer_expires_at: string | null;
};

export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;

  constructor(status: number, detail: string) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

const API_BASE_URL = (import.meta.env.VITE_API_URL ?? "").replace(/\/$/, "");
const TOKEN_KEY = "elevate.access_token";
const STAFF_TOKEN_KEY = "elevate.staff_access_token";

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token);
}

export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY);
}

export function getStaffToken(): string | null {
  return localStorage.getItem(STAFF_TOKEN_KEY);
}

export function setStaffToken(token: string): void {
  localStorage.setItem(STAFF_TOKEN_KEY, token);
}

export function clearStaffToken(): void {
  localStorage.removeItem(STAFF_TOKEN_KEY);
}

export function getStaffRole(): StaffRole | null {
  const token = getStaffToken();
  if (!token) return null;
  try {
    const encoded = token.split(".")[0].replace(/-/g, "+").replace(/_/g, "/");
    const padded = encoded + "=".repeat((4 - (encoded.length % 4)) % 4);
    const payload = JSON.parse(atob(padded));
    return payload.typ === "user" && ["owner", "receptionist", "trainer"].includes(payload.role)
      ? payload.role as StaffRole
      : null;
  } catch {
    return null;
  }
}

async function request<T>(
  path: string,
  options: RequestInit = {},
  authenticated = false,
  staff = false,
): Promise<T> {
  const headers = new Headers(options.headers);
  if (options.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const token = staff ? getStaffToken() : getToken();
  if (authenticated && token) {
    headers.set("Authorization", `Bearer ${token}`);
  }

  const response = await fetch(`${API_BASE_URL}${path}`, { ...options, headers });
  const body = await response.text();
  let parsed: unknown = null;
  if (body) {
    try {
      parsed = JSON.parse(body);
    } catch {
      parsed = body;
    }
  }
  if (!response.ok) {
    const detail =
      typeof parsed === "object" &&
      parsed !== null &&
      "detail" in parsed &&
      typeof parsed.detail === "string"
        ? parsed.detail
        : "Something went wrong. Please try again.";
    if (response.status === 401 && authenticated) {
      if (staff) {
        clearStaffToken();
        window.dispatchEvent(new Event("elevate:staff-unauthorized"));
      } else {
        clearToken();
        window.dispatchEvent(new Event("elevate:unauthorized"));
      }
    }
    throw new ApiError(response.status, detail);
  }
  return parsed as T;
}

export const api = {
  register: (payload: {
    full_name: string;
    phone_e164: string;
    email: string;
    password: string;
  }) => request<{ access_token: string }>("/api/auth/register", {
    method: "POST",
    body: JSON.stringify(payload),
  }),
  login: (payload: { email: string; password: string }) =>
    request<{ access_token: string }>("/api/auth/login", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  staffLogin: (payload: { email: string; password: string }) =>
    request<{ access_token: string }>("/api/auth/staff/login", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  profile: () => request<Profile>("/api/me", {}, true),
  services: () => request<Service[]>("/api/services"),
  trainers: () => request<Trainer[]>("/api/trainers"),
  sessions: () => request<Session[]>("/api/sessions?limit=100"),
  availability: (sessionId: string) =>
    request<Session>(`/api/sessions/${sessionId}/availability`),
  bookings: () => request<Booking[]>("/api/me/bookings", {}, true),
  passes: () => request<PassBalance[]>("/api/me/passes", {}, true),
  payments: () => request<Payment[]>("/api/me/payments", {}, true),
  invoices: () => request<Invoice[]>("/api/me/invoices", {}, true),
  createBooking: (payload: {
    session_id: string;
    customer_id: string;
    member_pass_id: string;
    idempotency_key: string;
  }) =>
    request<Booking>("/api/bookings", {
      method: "POST",
      body: JSON.stringify(payload),
    }, true),
  cancelBooking: (bookingId: string, customerId: string) =>
    request<{ booking: Booking; refunded: boolean }>(
      `/api/bookings/${bookingId}/cancel`,
      { method: "POST", body: JSON.stringify({ customer_id: customerId }) },
      true,
    ),
  rescheduleBooking: (
    bookingId: string,
    customerId: string,
    newSessionId: string,
  ) =>
    request<{ original_booking: Booking; replacement_booking: Booking }>(
      `/api/bookings/${bookingId}/reschedule`,
      {
        method: "POST",
        body: JSON.stringify({
          customer_id: customerId,
          new_session_id: newSessionId,
        }),
      },
      true,
    ),
  invoicePdf: async (invoiceId: string): Promise<Blob> => {
    const token = getToken();
    const response = await fetch(
      `${API_BASE_URL}/api/me/invoices/${invoiceId}/pdf`,
      { headers: token ? { Authorization: `Bearer ${token}` } : {} },
    );
    if (!response.ok) {
      if (response.status === 401) {
        clearToken();
        window.dispatchEvent(new Event("elevate:unauthorized"));
      }
      throw new ApiError(response.status, "Unable to download invoice.");
    }
    return response.blob();
  },
  admin: {
    dashboard: () => request<AdminDashboard>("/api/admin/dashboard", {}, true, true),
    customers: () => request<AdminCustomer[]>("/api/admin/customers", {}, true, true),
    createCustomer: (payload: {
      full_name: string;
      phone_e164: string;
      email?: string;
      whatsapp_opt_in?: boolean;
    }) => request<AdminCustomer>("/api/admin/customers", {
      method: "POST",
      body: JSON.stringify(payload),
    }, true, true),
    updateCustomer: (customerId: string, payload: Partial<{
      full_name: string;
      phone_e164: string;
      email: string;
      whatsapp_opt_in: boolean;
    }>) => request<AdminCustomer>(`/api/admin/customers/${customerId}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    }, true, true),
    services: () => request<AdminService[]>("/api/admin/services", {}, true, true),
    createService: (payload: {
      name: string;
      description?: string;
      duration_minutes: number;
      default_capacity: number;
      price_paise: number;
      tax_rate_bps?: number;
    }) => request<AdminService>("/api/admin/services", {
      method: "POST",
      body: JSON.stringify(payload),
    }, true, true),
    updateService: (serviceId: string, payload: Partial<{
      description: string;
      duration_minutes: number;
      default_capacity: number;
      price_paise: number;
      tax_rate_bps: number;
      is_active: boolean;
    }>) => request<AdminService>(`/api/admin/services/${serviceId}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    }, true, true),
    trainers: () => request<AdminTrainer[]>("/api/admin/trainers", {}, true, true),
    createTrainer: (payload: {
      full_name: string;
      phone_e164?: string;
      service_ids?: string[];
    }) => request<AdminTrainer & { service_ids?: string[] }>("/api/admin/trainers", {
      method: "POST",
      body: JSON.stringify(payload),
    }, true, true),
    passes: () => request<AdminPass[]>("/api/admin/passes", {}, true, true),
    passPlans: () => request<PassPlan[]>("/api/admin/pass-plans", {}, true, true),
    createPassPlan: (payload: {
      name: string;
      session_count: number;
      validity_days: number;
      price_paise: number;
      tax_rate_bps?: number;
    }) => request<PassPlan>("/api/admin/pass-plans", {
      method: "POST",
      body: JSON.stringify(payload),
    }, true, true),
    sessions: () => request<Session[]>("/api/sessions?limit=100"),
    createSession: (payload: {
      service_id: string;
      trainer_id: string;
      starts_at: string;
      ends_at: string;
      capacity?: number;
    }) => request<Record<string, unknown>>("/api/admin/sessions", {
      method: "POST",
      body: JSON.stringify(payload),
    }, true, true),
    bookings: () => request<AdminBooking[]>("/api/admin/bookings", {}, true, true),
    markAttendance: (payload: {
      booking_id: string;
      status: "present" | "absent" | "excused";
      notes?: string;
    }) => request<Record<string, unknown>>("/api/admin/attendance", {
      method: "POST",
      body: JSON.stringify(payload),
    }, true, true),
    waitlist: () => request<WaitlistEntry[]>("/api/admin/waitlist", {}, true, true),
    payments: () => request<AdminPayment[]>("/api/admin/payments", {}, true, true),
    invoices: () => request<AdminInvoice[]>("/api/admin/invoices", {}, true, true),
    settings: () => request<BusinessSetting[]>("/api/admin/settings", {}, true, true),
    updateSetting: (key: string, value: unknown) =>
      request<BusinessSetting>(`/api/admin/settings/${encodeURIComponent(key)}`, {
        method: "PATCH",
        body: JSON.stringify({ value }),
      }, true, true),
  },
};
