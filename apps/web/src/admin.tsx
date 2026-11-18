import { FormEvent, ReactNode, useEffect, useState } from "react";
import {
  api,
  ApiError,
  clearStaffToken,
  getStaffRole,
  getStaffToken,
  setStaffToken,
  StaffRole,
  AdminCustomer,
  AdminService,
  AdminTrainer,
  PassPlan,
  AdminPass,
  AdminBooking,
  AdminPayment,
  AdminInvoice,
  BusinessSetting,
  WaitlistEntry,
  Session,
  Service,
  Trainer,
} from "./api";

type AdminView =
  | "overview"
  | "customers"
  | "bookings"
  | "timetable"
  | "passes"
  | "payments"
  | "trainers"
  | "services"
  | "settings";

const navItems: Array<{ key: AdminView; label: string; icon: string }> = [
  { key: "overview", label: "Overview", icon: "⊞" },
  { key: "customers", label: "Customers", icon: "♙" },
  { key: "bookings", label: "Bookings", icon: "✓" },
  { key: "timetable", label: "Timetable", icon: "◷" },
  { key: "passes", label: "Passes & memberships", icon: "◎" },
  { key: "payments", label: "Payments & invoices", icon: "₹" },
  { key: "trainers", label: "Trainers", icon: "♟" },
  { key: "services", label: "Services", icon: "✦" },
  { key: "settings", label: "Settings", icon: "⚙" },
];

const money = (paise: number) =>
  new Intl.NumberFormat("en-IN", {
    style: "currency",
    currency: "INR",
    maximumFractionDigits: 0,
  }).format(paise / 100);

const dateTime = (value: string | undefined) =>
  value
    ? new Intl.DateTimeFormat("en-IN", {
        day: "numeric",
        month: "short",
        year: "numeric",
        hour: "numeric",
        minute: "2-digit",
      }).format(new Date(value))
    : "Not available";

export const viewFromPath = (): AdminView => {
  const segment = window.location.pathname.replace(/^\/admin\/?/, "").split("/")[0];
  return navItems.some((item) => item.key === segment)
    ? segment as AdminView
    : "overview";
};

export function AdminApp() {
  const [authenticated, setAuthenticated] = useState(Boolean(getStaffToken()));
  const [role, setRole] = useState<StaffRole | null>(getStaffRole());
  const [view, setView] = useState<AdminView>(viewFromPath());

  useEffect(() => {
    const sync = () => setView(viewFromPath());
    window.addEventListener("popstate", sync);
    return () => window.removeEventListener("popstate", sync);
  }, []);

  useEffect(() => {
    const unauthorized = () => {
      clearStaffToken();
      setAuthenticated(false);
      setRole(null);
    };
    window.addEventListener("elevate:staff-unauthorized", unauthorized);
    return () => window.removeEventListener("elevate:staff-unauthorized", unauthorized);
  }, []);

  const navigate = (next: AdminView) => {
    window.history.pushState({}, "", next === "overview" ? "/admin" : `/admin/${next}`);
    setView(next);
  };

  if (!authenticated || !role) return <AdminLogin onAuthenticated={(token) => {
    setStaffToken(token);
    setAuthenticated(true);
    setRole(getStaffRole());
  }} />;

  if (role === "trainer") {
    return (
      <StateScreen
        title="Admin access unavailable"
        body="Your trainer role does not have access to Elevate administration. Ask an owner or receptionist to sign in."
        action="Return to website"
        onAction={() => { window.location.href = "/"; }}
      />
    );
  }

  const logout = () => {
    clearStaffToken();
    setAuthenticated(false);
    setRole(null);
  };

  return (
    <div className="min-h-screen bg-[#f4f6f3] text-slate-950 lg:flex">
      <aside className="fixed bottom-0 left-0 right-0 z-30 border-t border-slate-200 bg-[#10231f] px-2 pb-2 pt-2 lg:bottom-auto lg:right-auto lg:top-0 lg:h-screen lg:w-[264px] lg:border-r lg:border-t-0 lg:px-4 lg:py-5">
        <div className="hidden px-3 pb-8 pt-2 lg:block">
          <button onClick={() => navigate("overview")} className="font-display text-2xl font-bold tracking-tight text-white">
            elevate<span className="text-lime-400">.</span>
          </button>
          <p className="mt-2 text-xs font-semibold uppercase tracking-[0.22em] text-emerald-200/60">Studio admin</p>
        </div>
        <nav className="flex justify-between gap-1 lg:block lg:space-y-1">
          {navItems.map((item) => (
            <button
              key={item.key}
              onClick={() => navigate(item.key)}
              title={item.label}
              className={`flex min-w-0 flex-1 flex-col items-center justify-center gap-1 rounded-xl px-1 py-2 text-[10px] font-semibold transition lg:w-full lg:flex-none lg:flex-row lg:justify-start lg:gap-3 lg:px-3 lg:py-2.5 lg:text-sm ${
                view === item.key
                  ? "bg-lime-300 text-emerald-950"
                  : "text-emerald-50/70 hover:bg-white/10 hover:text-white"
              }`}
            >
              <span className="text-base lg:w-5 lg:text-center">{item.icon}</span>
              <span className="truncate lg:truncate-none">{item.label}</span>
            </button>
          ))}
        </nav>
        <div className="hidden lg:absolute lg:bottom-5 lg:left-4 lg:right-4 lg:block">
          <a href="/" className="block rounded-xl px-3 py-2.5 text-sm font-semibold text-emerald-50/70 hover:bg-white/10 hover:text-white">← Customer website</a>
          <button onClick={logout} className="mt-1 block w-full rounded-xl px-3 py-2.5 text-left text-sm font-semibold text-rose-200 hover:bg-white/10">Sign out</button>
        </div>
      </aside>

      <main className="min-h-screen flex-1 pb-24 lg:ml-[264px] lg:pb-0">
        <header className="sticky top-0 z-20 flex h-16 items-center justify-between border-b border-slate-200 bg-white/95 px-4 backdrop-blur lg:px-8">
          <div>
            <p className="text-xs font-semibold uppercase tracking-[0.18em] text-emerald-700">Elevate Manager</p>
            <p className="text-sm font-bold capitalize text-slate-900">{view}</p>
          </div>
          <div className="flex items-center gap-2">
            <span className="hidden rounded-full bg-emerald-50 px-3 py-1.5 text-xs font-bold capitalize text-emerald-800 sm:block">{role}</span>
            <button onClick={logout} className="rounded-full border border-slate-200 p-2 text-sm text-slate-600 lg:hidden" title="Sign out">↪</button>
          </div>
        </header>
        <div className="mx-auto max-w-[1500px] p-4 sm:p-6 lg:p-8">
          {view === "overview" && <Overview role={role} />}
          {view === "customers" && <Customers role={role} />}
          {view === "bookings" && <Bookings />}
          {view === "timetable" && <Timetable role={role} />}
          {view === "passes" && <Passes role={role} />}
          {view === "payments" && <Payments />}
          {view === "trainers" && <Trainers role={role} />}
          {view === "services" && <Services role={role} />}
          {view === "settings" && <Settings role={role} />}
        </div>
      </main>
    </div>
  );
}

function AdminLogin({ onAuthenticated }: { onAuthenticated: (token: string) => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  return (
    <main className="flex min-h-screen items-center justify-center bg-[#10231f] px-5 py-10">
      <section className="w-full max-w-md rounded-[2rem] bg-white p-7 shadow-2xl sm:p-10">
        <button onClick={() => { window.location.href = "/"; }} className="font-display text-2xl font-bold text-emerald-950">
          elevate<span className="text-lime-500">.</span>
        </button>
        <p className="mt-10 text-xs font-bold uppercase tracking-[0.24em] text-emerald-700">Staff workspace</p>
        <h1 className="font-display mt-3 text-4xl tracking-tight text-slate-950">Welcome back.</h1>
        <p className="mt-3 text-sm leading-6 text-slate-500">Sign in with an authorized Elevate staff account.</p>
        <form
          className="mt-8 space-y-4"
          onSubmit={async (event) => {
            event.preventDefault();
            setBusy(true);
            setError("");
            const data = new FormData(event.currentTarget);
            try {
              const response = await api.staffLogin({
                email: String(data.get("email")),
                password: String(data.get("password")),
              });
              onAuthenticated(response.access_token);
            } catch (reason) {
              setError(reason instanceof Error ? reason.message : "Unable to sign in.");
            } finally {
              setBusy(false);
            }
          }}
        >
          <label className="block text-sm font-semibold text-slate-700">Email
            <input name="email" type="email" required className="input mt-2" />
          </label>
          <label className="block text-sm font-semibold text-slate-700">Password
            <input name="password" type="password" required className="input mt-2" />
          </label>
          {error && <div className="rounded-xl border border-rose-200 bg-rose-50 p-3 text-sm text-rose-800">{error}</div>}
          <button disabled={busy} className="button-primary w-full">{busy ? "Signing in…" : "Sign in"}</button>
        </form>
        <a href="/" className="mt-6 block text-center text-sm font-semibold text-emerald-800">← Return to customer website</a>
      </section>
    </main>
  );
}

function PageHeading({ title, body, action }: { title: string; body: string; action?: ReactNode }) {
  return (
    <div className="mb-6 flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
      <div>
        <h1 className="font-display text-3xl tracking-tight text-slate-950 sm:text-4xl">{title}</h1>
        <p className="mt-2 max-w-2xl text-sm leading-6 text-slate-500">{body}</p>
      </div>
      {action}
    </div>
  );
}

function LoadingBlock() {
  return <div className="flex min-h-[240px] items-center justify-center text-sm text-slate-500"><span className="mr-3 h-5 w-5 animate-spin rounded-full border-2 border-slate-200 border-t-emerald-700" />Loading…</div>;
}

function ErrorBlock({ error, retry }: { error: string; retry: () => void }) {
  return (
    <div className="rounded-2xl border border-rose-200 bg-rose-50 p-5">
      <p className="font-semibold text-rose-900">Unable to load this section</p>
      <p className="mt-1 text-sm text-rose-700">{error}</p>
      <button onClick={retry} className="button-secondary mt-4">Try again</button>
    </div>
  );
}

function EmptyBlock({ title, body }: { title: string; body: string }) {
  return <div className="rounded-2xl border border-dashed border-slate-300 bg-white p-8 text-center"><p className="font-display text-xl">{title}</p><p className="mt-2 text-sm text-slate-500">{body}</p></div>;
}

function StateScreen({ title, body, action, onAction }: { title: string; body: string; action: string; onAction: () => void }) {
  return (
    <main className="flex min-h-screen items-center justify-center bg-[#10231f] px-5 text-white">
      <div className="max-w-md text-center">
        <h1 className="font-display text-4xl">{title}</h1>
        <p className="mt-4 leading-7 text-emerald-50/70">{body}</p>
        <button onClick={onAction} className="button-lime mt-7">{action}</button>
      </div>
    </main>
  );
}

function Table({ headers, rows }: { headers: string[]; rows: ReactNode[][] }) {
  return (
    <div className="overflow-x-auto rounded-2xl border border-slate-200 bg-white">
      <table className="w-full min-w-[720px] text-left text-sm">
        <thead><tr className="border-b border-slate-200 bg-slate-50 text-xs uppercase tracking-wider text-slate-500">{headers.map((header) => <th key={header} className="px-4 py-3 font-bold">{header}</th>)}</tr></thead>
        <tbody>{rows.map((row, index) => <tr key={index} className="border-b border-slate-100 last:border-0 hover:bg-slate-50/70">{row.map((cell, cellIndex) => <td key={cellIndex} className="px-4 py-3 align-middle">{cell}</td>)}</tr>)}</tbody>
      </table>
    </div>
  );
}

function Overview({ role }: { role: StaffRole }) {
  const [data, setData] = useState<Awaited<ReturnType<typeof api.admin.dashboard>> | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const refresh = async () => {
    setLoading(true);
    setError("");
    try { setData(await api.admin.dashboard()); }
    catch (reason) { setError(reason instanceof ApiError ? reason.detail : "Unable to load dashboard metrics."); }
    finally { setLoading(false); }
  };
  useEffect(() => { void refresh(); }, []);
  return (
    <>
      <PageHeading title="Studio overview" body={`Live operational metrics for the ${role} workspace.`} />
      {loading ? <LoadingBlock /> : error ? <ErrorBlock error={error} retry={() => void refresh()} /> : data && (
        <>
          <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-5">
            {[
              ["Customers", data.customers, "Active profiles"],
              ["Confirmed bookings", data.confirmed_bookings, "All-time confirmed"],
              ["Pending payments", data.pending_payments, "Requires attention"],
              ["Active passes", data.active_passes, "Current memberships"],
              ["Active waitlist", data.active_waitlist, "Waiting or offered"],
            ].map(([label, value, detail]) => (
              <div key={String(label)} className="rounded-2xl border border-slate-200 bg-white p-5">
                <p className="text-xs font-bold uppercase tracking-wider text-slate-400">{label}</p>
                <p className="font-display mt-4 text-4xl text-slate-950">{value}</p>
                <p className="mt-1 text-xs text-slate-500">{detail}</p>
              </div>
            ))}
          </div>
          <div className="mt-6 grid gap-4 lg:grid-cols-2">
            <div className="rounded-2xl border border-slate-200 bg-white p-5"><p className="text-xs font-bold uppercase tracking-wider text-emerald-700">Operations</p><p className="mt-3 font-display text-2xl">Bookings and payments</p><p className="mt-2 text-sm leading-6 text-slate-500">Use Bookings for live schedules and Payments & invoices for financial records.</p></div>
            <div className="rounded-2xl border border-slate-200 bg-white p-5"><p className="text-xs font-bold uppercase tracking-wider text-emerald-700">Membership</p><p className="mt-3 font-display text-2xl">Pass activity</p><p className="mt-2 text-sm leading-6 text-slate-500">Pass assignments and credit changes are not currently exposed by admin APIs.</p></div>
          </div>
        </>
      )}
    </>
  );
}

function Customers({ role }: { role: StaffRole }) {
  const [items, setItems] = useState<AdminCustomer[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState<AdminCustomer | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const refresh = async () => {
    setLoading(true); setError("");
    try { setItems(await api.admin.customers()); }
    catch (reason) { setError(reason instanceof ApiError ? reason.detail : "Unable to load customers."); }
    finally { setLoading(false); }
  };
  useEffect(() => { void refresh(); }, []);
  const save = async (payload: { full_name: string; phone_e164: string; email?: string; whatsapp_opt_in?: boolean }) => {
    if (selected) await api.admin.updateCustomer(selected.id, payload);
    else await api.admin.createCustomer(payload);
    setSelected(null); setCreateOpen(false); await refresh();
  };
  return (
    <>
      <PageHeading title="Customers" body="View and maintain studio customer profiles." action={<button onClick={() => { setSelected(null); setCreateOpen(true); }} className="button-primary">＋ Add customer</button>} />
      {loading ? <LoadingBlock /> : error ? <ErrorBlock error={error} retry={() => void refresh()} /> : items.length === 0 ? <EmptyBlock title="No customers" body="Customer profiles will appear here." /> : (
        <Table
          headers={["Customer", "Contact", "WhatsApp", "Joined", ""]}
          rows={items.map((customer) => [
            <div key="name"><p className="font-semibold">{customer.full_name}</p><p className="mt-0.5 text-xs text-slate-400">{customer.id.slice(0, 8)}</p></div>,
            <div key="contact"><p>{customer.email || "No email"}</p><p className="mt-0.5 text-xs text-slate-500">{customer.phone_e164}</p></div>,
            <span key="wa" className={`rounded-full px-2.5 py-1 text-xs font-bold ${customer.whatsapp_opt_in ? "bg-emerald-50 text-emerald-700" : "bg-slate-100 text-slate-500"}`}>{customer.whatsapp_opt_in ? "Enabled" : "Disabled"}</span>,
            <span key="date" className="text-slate-600">{dateTime(customer.created_at)}</span>,
            <button key="action" onClick={() => { setSelected(customer); setCreateOpen(true); }} className="text-sm font-bold text-emerald-800">View / edit</button>,
          ])}
        />
      )}
      {createOpen && <CustomerModal customer={selected} onClose={() => { setCreateOpen(false); setSelected(null); }} onSave={save} />}
    </>
  );
}

function CustomerModal({ customer, onClose, onSave }: { customer: AdminCustomer | null; onClose: () => void; onSave: (payload: { full_name: string; phone_e164: string; email?: string; whatsapp_opt_in?: boolean }) => Promise<void> }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  return (
    <Modal title={customer ? "Customer details" : "New customer"} onClose={onClose}>
      <form onSubmit={async (event) => {
        event.preventDefault(); setBusy(true); setError("");
        const data = new FormData(event.currentTarget);
        try {
          await onSave({
            full_name: String(data.get("full_name")),
            phone_e164: String(data.get("phone_e164")),
            email: String(data.get("email")) || undefined,
            whatsapp_opt_in: data.get("whatsapp_opt_in") === "on",
          });
        } catch (reason) { setError(reason instanceof Error ? reason.message : "Unable to save customer."); }
        finally { setBusy(false); }
      }} className="mt-5 space-y-4">
        <FormField label="Full name" name="full_name" defaultValue={customer?.full_name} required />
        <FormField label="Mobile number" name="phone_e164" defaultValue={customer?.phone_e164} placeholder="+919876543210" required />
        <FormField label="Email" name="email" type="email" defaultValue={customer?.email ?? ""} />
        <label className="flex items-center gap-3 rounded-xl border border-slate-200 p-3 text-sm font-semibold"><input name="whatsapp_opt_in" type="checkbox" defaultChecked={Boolean(customer?.whatsapp_opt_in)} />WhatsApp updates</label>
        {error && <div className="rounded-xl border border-rose-200 bg-rose-50 p-3 text-sm text-rose-700">{error}</div>}
        <button disabled={busy} className="button-primary w-full">{busy ? "Saving…" : "Save customer"}</button>
      </form>
    </Modal>
  );
}

function Bookings() {
  const [items, setItems] = useState<AdminBooking[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState<AdminBooking | null>(null);
  const refresh = async () => {
    setLoading(true); setError("");
    try { setItems(await api.admin.bookings()); }
    catch (reason) { setError(reason instanceof ApiError ? reason.detail : "Unable to load bookings."); }
    finally { setLoading(false); }
  };
  useEffect(() => { void refresh(); }, []);
  return (
    <>
      <PageHeading title="Bookings" body="View every studio booking and mark attendance. Admin cancellation and rescheduling are not exposed by the backend." />
      {loading ? <LoadingBlock /> : error ? <ErrorBlock error={error} retry={() => void refresh()} /> : items.length === 0 ? <EmptyBlock title="No bookings" body="Bookings will appear after customers reserve sessions." /> : (
        <Table headers={["Booking", "Customer", "Session", "Payment", "Status", ""]} rows={items.map((booking) => [
          <div key="ref"><p className="font-semibold">{booking.booking_ref}</p><p className="text-xs text-slate-400">{booking.id.slice(0, 8)}</p></div>,
          <span key="customer" className="font-semibold">{booking.customer_name}</span>,
          <div key="session"><p>{booking.service_name}</p><p className="mt-0.5 text-xs text-slate-500">{dateTime(booking.starts_at)}</p></div>,
          <span key="payment" className="capitalize">{booking.payment_mode}</span>,
          <span key="status" className="rounded-full bg-slate-100 px-2.5 py-1 text-xs font-bold capitalize">{booking.status}</span>,
          <button key="attendance" onClick={() => setSelected(booking)} className="text-sm font-bold text-emerald-800">Attendance</button>,
        ])} />
      )}
      {selected && <AttendanceModal booking={selected} onClose={() => setSelected(null)} />}
    </>
  );
}

function AttendanceModal({ booking, onClose }: { booking: AdminBooking; onClose: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  return (
    <Modal title="Mark attendance" onClose={onClose}>
      <p className="mt-4 rounded-xl bg-slate-50 p-3 text-sm"><strong>{booking.customer_name}</strong><br /><span className="text-slate-500">{booking.service_name} · {dateTime(booking.starts_at)}</span></p>
      <form onSubmit={async (event) => {
        event.preventDefault(); setBusy(true); setError("");
        const data = new FormData(event.currentTarget);
        try {
          await api.admin.markAttendance({
            booking_id: booking.id,
            status: String(data.get("status")) as "present" | "absent" | "excused",
            notes: String(data.get("notes")) || undefined,
          });
          onClose();
        } catch (reason) { setError(reason instanceof Error ? reason.message : "Unable to mark attendance."); }
        finally { setBusy(false); }
      }} className="mt-5 space-y-4">
        <label className="block text-sm font-semibold">Status<select name="status" className="input mt-2"><option value="present">Present</option><option value="absent">Absent</option><option value="excused">Excused</option></select></label>
        <label className="block text-sm font-semibold">Notes<textarea name="notes" className="input mt-2 min-h-24" /></label>
        {error && <div className="rounded-xl border border-rose-200 bg-rose-50 p-3 text-sm text-rose-700">{error}</div>}
        <button disabled={busy} className="button-primary w-full">{busy ? "Saving…" : "Save attendance"}</button>
      </form>
    </Modal>
  );
}

function Timetable({ role }: { role: StaffRole }) {
  const [sessions, setSessions] = useState<Session[]>([]);
  const [services, setServices] = useState<AdminService[]>([]);
  const [trainers, setTrainers] = useState<AdminTrainer[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [createOpen, setCreateOpen] = useState(false);
  const refresh = async () => {
    setLoading(true); setError("");
    try {
      const [sessionData, serviceData, trainerData] = await Promise.all([
        api.admin.sessions(),
        api.admin.services(),
        api.admin.trainers(),
      ]);
      setSessions(sessionData); setServices(serviceData); setTrainers(trainerData);
    } catch (reason) { setError(reason instanceof ApiError ? reason.detail : "Unable to load timetable."); }
    finally { setLoading(false); }
  };
  useEffect(() => { void refresh(); }, []);
  return (
    <>
      <PageHeading title="Timetable" body="Review upcoming sessions and schedule new studio classes." action={<button onClick={() => setCreateOpen(true)} className="button-primary">＋ Add session</button>} />
      {loading ? <LoadingBlock /> : error ? <ErrorBlock error={error} retry={() => void refresh()} /> : sessions.length === 0 ? <EmptyBlock title="No upcoming sessions" body="Create a session to build the timetable." /> : (
        <Table headers={["Session", "Trainer", "Time", "Capacity", "Availability"]} rows={sessions.map((session) => [
          <span key="service" className="font-semibold">{session.service_name}</span>,
          <span key="trainer">{session.trainer_name}</span>,
          <div key="time"><p>{dateTime(session.starts_at)}</p><p className="text-xs text-slate-500">to {dateTime(session.ends_at)}</p></div>,
          <span key="capacity">{session.capacity}</span>,
          <span key="available" className={session.available_seats > 0 ? "font-bold text-emerald-700" : "font-bold text-rose-700"}>{session.available_seats} open</span>,
        ])} />
      )}
      {createOpen && <SessionModal services={services} trainers={trainers} onClose={() => setCreateOpen(false)} onSaved={() => void refresh()} />}
    </>
  );
}

function SessionModal({ services, trainers, onClose, onSaved }: { services: AdminService[]; trainers: AdminTrainer[]; onClose: () => void; onSaved: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  return (
    <Modal title="Add session" onClose={onClose}>
      <form onSubmit={async (event) => {
        event.preventDefault();
        setBusy(true);
        setError("");
        const data = new FormData(event.currentTarget);
        try {
          const capacityValue = String(data.get("capacity"));
          await api.admin.createSession({
            service_id: String(data.get("service_id")),
            trainer_id: String(data.get("trainer_id")),
            starts_at: new Date(String(data.get("starts_at"))).toISOString(),
            ends_at: new Date(String(data.get("ends_at"))).toISOString(),
            ...(capacityValue ? { capacity: Number(capacityValue) } : {}),
          });
          onSaved();
          onClose();
        } catch (reason) {
          setError(reason instanceof Error ? reason.message : "Unable to create session.");
        } finally {
          setBusy(false);
        }
      }} className="mt-5 space-y-4">
        <label className="block text-sm font-semibold">Service<select name="service_id" className="input mt-2" required>{services.map((service) => <option key={service.id} value={service.id}>{service.name}</option>)}</select></label>
        <label className="block text-sm font-semibold">Trainer<select name="trainer_id" className="input mt-2" required>{trainers.map((trainer) => <option key={trainer.id} value={trainer.id}>{trainer.full_name}</option>)}</select></label>
        <FormField label="Starts at" name="starts_at" type="datetime-local" required />
        <FormField label="Ends at" name="ends_at" type="datetime-local" required />
        <FormField label="Capacity (optional)" name="capacity" type="number" />
        {error && <div className="rounded-xl border border-rose-200 bg-rose-50 p-3 text-sm text-rose-700">{error}</div>}
        <button disabled={busy} className="button-primary w-full">{busy ? "Creating…" : "Create session"}</button>
      </form>
    </Modal>
  );
}

function Passes({ role }: { role: StaffRole }) {
  const [passes, setPasses] = useState<AdminPass[]>([]);
  const [plans, setPlans] = useState<PassPlan[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [createOpen, setCreateOpen] = useState(false);
  const refresh = async () => {
    setLoading(true); setError("");
    try {
      const [passData, planData] = await Promise.all([api.admin.passes(), api.admin.passPlans()]);
      setPasses(passData); setPlans(planData);
    } catch (reason) { setError(reason instanceof ApiError ? reason.detail : "Unable to load passes."); }
    finally { setLoading(false); }
  };
  useEffect(() => { void refresh(); }, []);
  return (
    <>
      <PageHeading title="Passes & memberships" body="View pass plans and customer balances. Assignment and credit adjustment APIs are not available." action={role === "owner" ? <button onClick={() => setCreateOpen(true)} className="button-primary">＋ Add pass plan</button> : undefined} />
      {loading ? <LoadingBlock /> : error ? <ErrorBlock error={error} retry={() => void refresh()} /> : (
        <div className="space-y-8">
          <section>
            <h2 className="font-display mb-3 text-2xl">Pass plans</h2>
            {plans.length ? <Table headers={["Plan", "Sessions", "Validity", "Price", "Status"]} rows={plans.map((plan) => [
              <span key="name" className="font-semibold">{plan.name}</span>,
              <span key="sessions">{plan.session_count}</span>,
              <span key="validity">{plan.validity_days} days</span>,
              <span key="price">{money(plan.price_paise)}</span>,
              <span key="status" className="rounded-full bg-slate-100 px-2.5 py-1 text-xs font-bold">{plan.is_active ? "Active" : "Inactive"}</span>,
            ])} /> : <EmptyBlock title="No pass plans" body="Create a pass plan to establish membership options." />}
          </section>
          <section>
            <h2 className="font-display mb-3 text-2xl">Customer passes</h2>
            {passes.length ? <Table headers={["Customer", "Plan", "Balance", "Expiry", "Status"]} rows={passes.map((pass) => [
              <span key="customer" className="font-semibold">{pass.customer_name}</span>,
              <span key="plan">{pass.plan_name}</span>,
              <span key="balance" className="font-bold text-emerald-800">{pass.credits_remaining} / {pass.sessions_total}</span>,
              <span key="expiry">{dateTime(pass.expires_at)}</span>,
              <span key="status" className="rounded-full bg-slate-100 px-2.5 py-1 text-xs font-bold capitalize">{pass.status}</span>,
            ])} /> : <EmptyBlock title="No assigned passes" body="No customer passes have been created in the database." />}
          </section>
        </div>
      )}
      {createOpen && <PassPlanModal onClose={() => setCreateOpen(false)} onSaved={() => void refresh()} />}
    </>
  );
}

function PassPlanModal({ onClose, onSaved }: { onClose: () => void; onSaved: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  return (
    <Modal title="Add pass plan" onClose={onClose}>
      <form onSubmit={async (event) => {
        event.preventDefault(); setBusy(true); setError("");
        const data = new FormData(event.currentTarget);
        try {
          await api.admin.createPassPlan({
            name: String(data.get("name")),
            session_count: Number(data.get("session_count")),
            validity_days: Number(data.get("validity_days")),
            price_paise: Number(data.get("price_paise")),
            tax_rate_bps: Number(data.get("tax_rate_bps")) || 0,
          });
          onSaved(); onClose();
        } catch (reason) { setError(reason instanceof Error ? reason.message : "Unable to create pass plan."); }
        finally { setBusy(false); }
      }} className="mt-5 space-y-4">
        <FormField label="Plan name" name="name" required />
        <FormField label="Session count" name="session_count" type="number" required />
        <FormField label="Validity in days" name="validity_days" type="number" required />
        <FormField label="Price in paise" name="price_paise" type="number" required />
        <FormField label="Tax rate in bps" name="tax_rate_bps" type="number" defaultValue="0" />
        {error && <div className="rounded-xl border border-rose-200 bg-rose-50 p-3 text-sm text-rose-700">{error}</div>}
        <button disabled={busy} className="button-primary w-full">{busy ? "Saving…" : "Create pass plan"}</button>
      </form>
    </Modal>
  );
}

function Payments() {
  const [payments, setPayments] = useState<AdminPayment[]>([]);
  const [invoices, setInvoices] = useState<AdminInvoice[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const refresh = async () => {
    setLoading(true); setError("");
    try {
      const [paymentData, invoiceData] = await Promise.all([api.admin.payments(), api.admin.invoices()]);
      setPayments(paymentData); setInvoices(invoiceData);
    } catch (reason) { setError(reason instanceof ApiError ? reason.detail : "Unable to load financial records."); }
    finally { setLoading(false); }
  };
  useEffect(() => { void refresh(); }, []);
  return (
    <>
      <PageHeading title="Payments & invoices" body="Read-only financial records provided by the backend." />
      {loading ? <LoadingBlock /> : error ? <ErrorBlock error={error} retry={() => void refresh()} /> : (
        <div className="grid gap-8 xl:grid-cols-2">
          <section>
            <h2 className="font-display mb-3 text-2xl">Payments</h2>
            {payments.length ? <Table headers={["Customer", "Invoice", "Amount", "Method", "Received"]} rows={payments.map((payment) => [
              <span key="customer" className="font-semibold">{payment.customer_name}</span>,
              <div key="invoice"><p>{payment.invoice_number}</p><p className="text-xs capitalize text-slate-500">{payment.kind}</p></div>,
              <span key="amount" className="font-bold">{money(payment.amount_paise)}</span>,
              <span key="method" className="capitalize">{payment.method}</span>,
              <span key="date">{dateTime(payment.received_at)}</span>,
            ])} /> : <EmptyBlock title="No payments" body="No payment transactions have been recorded." />}
          </section>
          <section>
            <h2 className="font-display mb-3 text-2xl">Invoices</h2>
            {invoices.length ? <Table headers={["Customer", "Invoice", "Total", "Balance", "Status"]} rows={invoices.map((invoice) => [
              <span key="customer" className="font-semibold">{invoice.customer_name}</span>,
              <div key="invoice"><p>{invoice.invoice_number}</p><p className="text-xs text-slate-500">{dateTime(invoice.issue_date)}</p></div>,
              <span key="total">{money(invoice.total_paise)}</span>,
              <span key="balance" className="font-bold">{money(invoice.balance_paise)}</span>,
              <span key="status" className="rounded-full bg-slate-100 px-2.5 py-1 text-xs font-bold capitalize">{invoice.payment_status.replaceAll("_", " ")}</span>,
            ])} /> : <EmptyBlock title="No invoices" body="No invoices have been issued." />}
          </section>
        </div>
      )}
    </>
  );
}

function Trainers({ role }: { role: StaffRole }) {
  const [items, setItems] = useState<AdminTrainer[]>([]);
  const [services, setServices] = useState<AdminService[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [createOpen, setCreateOpen] = useState(false);
  const refresh = async () => {
    setLoading(true); setError("");
    try {
      const [trainerData, serviceData] = await Promise.all([api.admin.trainers(), api.admin.services()]);
      setItems(trainerData); setServices(serviceData);
    } catch (reason) { setError(reason instanceof ApiError ? reason.detail : "Unable to load trainers."); }
    finally { setLoading(false); }
  };
  useEffect(() => { void refresh(); }, []);
  return (
    <>
      <PageHeading title="Trainers" body="View active and inactive trainer profiles." action={role === "owner" ? <button onClick={() => setCreateOpen(true)} className="button-primary">＋ Add trainer</button> : undefined} />
      {loading ? <LoadingBlock /> : error ? <ErrorBlock error={error} retry={() => void refresh()} /> : items.length === 0 ? <EmptyBlock title="No trainers" body="No trainer profiles are available." /> : (
        <Table headers={["Trainer", "Phone", "Status"]} rows={items.map((trainer) => [
          <span key="name" className="font-semibold">{trainer.full_name}</span>,
          <span key="phone">{trainer.phone_e164 || "Not provided"}</span>,
          <span key="status" className={`rounded-full px-2.5 py-1 text-xs font-bold ${trainer.is_active ? "bg-emerald-50 text-emerald-700" : "bg-slate-100 text-slate-500"}`}>{trainer.is_active ? "Active" : "Inactive"}</span>,
        ])} />
      )}
      {createOpen && <TrainerModal services={services} onClose={() => setCreateOpen(false)} onSaved={() => void refresh()} />}
    </>
  );
}

function TrainerModal({ services, onClose, onSaved }: { services: AdminService[]; onClose: () => void; onSaved: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  return (
    <Modal title="Add trainer" onClose={onClose}>
      <form onSubmit={async (event) => {
        event.preventDefault(); setBusy(true); setError("");
        const data = new FormData(event.currentTarget);
        try {
          await api.admin.createTrainer({
            full_name: String(data.get("full_name")),
            phone_e164: String(data.get("phone_e164")) || undefined,
            service_ids: data.getAll("service_ids").map(String),
          });
          onSaved(); onClose();
        } catch (reason) { setError(reason instanceof Error ? reason.message : "Unable to create trainer."); }
        finally { setBusy(false); }
      }} className="mt-5 space-y-4">
        <FormField label="Full name" name="full_name" required />
        <FormField label="Phone number" name="phone_e164" placeholder="+919876543210" />
        <fieldset><legend className="text-sm font-semibold">Mapped services</legend><div className="mt-2 grid gap-2 sm:grid-cols-2">{services.map((service) => <label key={service.id} className="flex items-center gap-2 rounded-xl border border-slate-200 p-3 text-sm"><input type="checkbox" name="service_ids" value={service.id} />{service.name}</label>)}</div></fieldset>
        {error && <div className="rounded-xl border border-rose-200 bg-rose-50 p-3 text-sm text-rose-700">{error}</div>}
        <button disabled={busy} className="button-primary w-full">{busy ? "Saving…" : "Create trainer"}</button>
      </form>
    </Modal>
  );
}

function Services({ role }: { role: StaffRole }) {
  const [items, setItems] = useState<AdminService[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState<AdminService | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const refresh = async () => {
    setLoading(true); setError("");
    try { setItems(await api.admin.services()); }
    catch (reason) { setError(reason instanceof ApiError ? reason.detail : "Unable to load services."); }
    finally { setLoading(false); }
  };
  useEffect(() => { void refresh(); }, []);
  return (
    <>
      <PageHeading title="Services" body="Manage the studio service catalog." action={role === "owner" ? <button onClick={() => { setSelected(null); setCreateOpen(true); }} className="button-primary">＋ Add service</button> : undefined} />
      {loading ? <LoadingBlock /> : error ? <ErrorBlock error={error} retry={() => void refresh()} /> : items.length === 0 ? <EmptyBlock title="No services" body="No studio services are configured." /> : (
        <Table headers={["Service", "Duration", "Capacity", "Price", "Status", ""]} rows={items.map((service) => [
          <div key="name"><p className="font-semibold">{service.name}</p><p className="mt-0.5 max-w-xs truncate text-xs text-slate-500">{service.description || "No description"}</p></div>,
          <span key="duration">{service.duration_minutes} min</span>,
          <span key="capacity">{service.default_capacity}</span>,
          <span key="price" className="font-bold">{money(service.price_paise)}</span>,
          <span key="status" className={`rounded-full px-2.5 py-1 text-xs font-bold ${service.is_active ? "bg-emerald-50 text-emerald-700" : "bg-slate-100 text-slate-500"}`}>{service.is_active ? "Active" : "Inactive"}</span>,
          role === "owner" ? <button key="edit" onClick={() => { setSelected(service); setCreateOpen(true); }} className="text-sm font-bold text-emerald-800">Edit</button> : <span key="readonly" className="text-xs text-slate-400">Owner only</span>,
        ])} />
      )}
      {createOpen && role === "owner" && <ServiceModal service={selected} onClose={() => { setCreateOpen(false); setSelected(null); }} onSaved={() => void refresh()} />}
    </>
  );
}

function ServiceModal({ service, onClose, onSaved }: { service: AdminService | null; onClose: () => void; onSaved: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  return (
    <Modal title={service ? "Edit service" : "Add service"} onClose={onClose}>
      <form onSubmit={async (event) => {
        event.preventDefault(); setBusy(true); setError("");
        const data = new FormData(event.currentTarget);
        try {
          if (service) {
            await api.admin.updateService(service.id, {
              description: String(data.get("description")),
              duration_minutes: Number(data.get("duration_minutes")),
              default_capacity: Number(data.get("default_capacity")),
              price_paise: Number(data.get("price_paise")),
              tax_rate_bps: Number(data.get("tax_rate_bps")) || 0,
              is_active: data.get("is_active") === "on",
            });
          } else {
            await api.admin.createService({
              name: String(data.get("name")),
              description: String(data.get("description")) || undefined,
              duration_minutes: Number(data.get("duration_minutes")),
              default_capacity: Number(data.get("default_capacity")),
              price_paise: Number(data.get("price_paise")),
              tax_rate_bps: Number(data.get("tax_rate_bps")) || 0,
            });
          }
          onSaved(); onClose();
        } catch (reason) { setError(reason instanceof Error ? reason.message : "Unable to save service."); }
        finally { setBusy(false); }
      }} className="mt-5 space-y-4">
        {!service && <FormField label="Service name" name="name" required />}
        <label className="block text-sm font-semibold">Description<textarea name="description" className="input mt-2 min-h-20" defaultValue={service?.description ?? ""} /></label>
        <FormField label="Duration in minutes" name="duration_minutes" type="number" defaultValue={String(service?.duration_minutes ?? "")} required />
        <FormField label="Default capacity" name="default_capacity" type="number" defaultValue={String(service?.default_capacity ?? "")} required />
        <FormField label="Price in paise" name="price_paise" type="number" defaultValue={String(service?.price_paise ?? "")} required />
        <FormField label="Tax rate in bps" name="tax_rate_bps" type="number" defaultValue={String(service?.tax_rate_bps ?? 0)} />
        {service && <label className="flex items-center gap-3 rounded-xl border border-slate-200 p-3 text-sm font-semibold"><input name="is_active" type="checkbox" defaultChecked={service.is_active} />Service is active</label>}
        {error && <div className="rounded-xl border border-rose-200 bg-rose-50 p-3 text-sm text-rose-700">{error}</div>}
        <button disabled={busy} className="button-primary w-full">{busy ? "Saving…" : "Save service"}</button>
      </form>
    </Modal>
  );
}

function Settings({ role }: { role: StaffRole }) {
  const [items, setItems] = useState<BusinessSetting[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const refresh = async () => {
    setLoading(true); setError("");
    try { setItems(await api.admin.settings()); }
    catch (reason) { setError(reason instanceof ApiError ? reason.detail : "Unable to load settings."); }
    finally { setLoading(false); }
  };
  useEffect(() => { void refresh(); }, []);
  return (
    <>
      <PageHeading title="Settings" body={role === "owner" ? "View and update supported business settings." : "View business settings. Updates are restricted to owners."} />
      {loading ? <LoadingBlock /> : error ? <ErrorBlock error={error} retry={() => void refresh()} /> : items.length === 0 ? <EmptyBlock title="No settings" body="No business settings are configured." /> : (
        <div className="grid gap-4 md:grid-cols-2">
          {items.map((setting) => <SettingCard key={setting.key} setting={setting} owner={role === "owner"} onSaved={() => void refresh()} />)}
        </div>
      )}
    </>
  );
}

function SettingCard({ setting, owner, onSaved }: { setting: BusinessSetting; owner: boolean; onSaved: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const serialized = typeof setting.value === "string" ? setting.value : JSON.stringify(setting.value);
  return (
    <form onSubmit={async (event) => {
      event.preventDefault(); if (!owner) return;
      setBusy(true); setError("");
      const data = new FormData(event.currentTarget);
      const raw = String(data.get("value"));
      let value: unknown = raw;
      try { value = JSON.parse(raw); } catch {}
      try { await api.admin.updateSetting(setting.key, value); onSaved(); }
      catch (reason) { setError(reason instanceof Error ? reason.message : "Unable to update setting."); }
      finally { setBusy(false); }
    }} className="rounded-2xl border border-slate-200 bg-white p-5">
      <p className="text-xs font-bold uppercase tracking-wider text-emerald-700">{setting.key}</p>
      <textarea name="value" defaultValue={serialized} disabled={!owner} className="input mt-3 min-h-24 font-mono text-sm" />
      {error && <p className="mt-2 text-sm text-rose-700">{error}</p>}
      {owner && <button disabled={busy} className="button-secondary mt-3">{busy ? "Saving…" : "Update setting"}</button>}
    </form>
  );
}

function FormField({ label, name, type = "text", ...props }: { label: string; name: string; type?: string; [key: string]: unknown }) {
  return <label className="block text-sm font-semibold text-slate-700">{label}<input name={name} type={type} className="input mt-2" {...props} /></label>;
}

function Modal({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-slate-950/50 p-0 sm:items-center sm:p-5">
      <div className="max-h-[92vh] w-full max-w-lg overflow-y-auto rounded-t-[2rem] bg-white p-6 shadow-2xl sm:rounded-[2rem]">
        <div className="flex items-center justify-between"><h2 className="font-display text-3xl">{title}</h2><button onClick={onClose} aria-label="Close" className="text-2xl text-slate-400">×</button></div>
        {children}
      </div>
    </div>
  );
}
