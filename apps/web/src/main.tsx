import { FormEvent, ReactNode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  api,
  ApiError,
  Booking,
  clearToken,
  getToken,
  Invoice,
  PassBalance,
  Profile,
  Service,
  Session,
  setToken,
  Trainer,
} from "./api";
import { AdminApp } from "./admin";
import "./styles.css";

type View = "overview" | "schedule" | "bookings" | "finances" | "profile";
type Catalog = { services: Service[]; trainers: Trainer[]; sessions: Session[] };
type Account = {
  profile: Profile;
  bookings: Booking[];
  passes: PassBalance[];
  payments: Awaited<ReturnType<typeof api.payments>>;
  invoices: Invoice[];
};

const money = (paise: number) =>
  new Intl.NumberFormat("en-IN", {
    style: "currency",
    currency: "INR",
    maximumFractionDigits: 0,
  }).format(paise / 100);

const dateTime = (value: string | undefined) =>
  value
    ? new Intl.DateTimeFormat("en-IN", {
        weekday: "short",
        day: "numeric",
        month: "short",
        hour: "numeric",
        minute: "2-digit",
      }).format(new Date(value))
    : "Date unavailable";

const dateOnly = (value: string) =>
  new Intl.DateTimeFormat("en-IN", {
    day: "numeric",
    month: "short",
    year: "numeric",
  }).format(new Date(value));

function App() {
  const [authenticated, setAuthenticated] = useState(Boolean(getToken()));
  const [view, setView] = useState<View>("overview");
  const [authMode, setAuthMode] = useState<"login" | "register">("login");
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [account, setAccount] = useState<Account | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const loadCatalog = async () => {
    const [services, trainers, sessions] = await Promise.all([
      api.services(),
      api.trainers(),
      api.sessions(),
    ]);
    setCatalog({ services, trainers, sessions });
  };

  const loadAccount = async () => {
    const [profile, bookings, passes, payments, invoices] = await Promise.all([
      api.profile(),
      api.bookings(),
      api.passes(),
      api.payments(),
      api.invoices(),
    ]);
    setAccount({ profile, bookings, passes, payments, invoices });
  };

  const refresh = async () => {
    setLoading(true);
    setError("");
    try {
      await loadCatalog();
      if (getToken()) {
        await loadAccount();
        setAuthenticated(true);
      }
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 401) {
        setAuthenticated(false);
        setAccount(null);
      } else {
        setError(reason instanceof Error ? reason.message : "Unable to load Elevate.");
      }
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    const handleUnauthorized = () => {
      setAuthenticated(false);
      setAccount(null);
      setAuthMode("login");
    };
    window.addEventListener("elevate:unauthorized", handleUnauthorized);
    void refresh();
    return () => window.removeEventListener("elevate:unauthorized", handleUnauthorized);
  }, []);

  const handleAuthenticated = async (token: string) => {
    setToken(token);
    setAuthenticated(true);
    setError("");
    try {
      await loadAccount();
    } catch (reason) {
      clearToken();
      setAuthenticated(false);
      setError(reason instanceof Error ? reason.message : "Unable to load your account.");
    }
  };

  const logout = () => {
    clearToken();
    setAccount(null);
    setAuthenticated(false);
    setView("overview");
  };

  if (!authenticated) {
    return (
      <AuthScreen
        mode={authMode}
        onModeChange={setAuthMode}
        onAuthenticated={handleAuthenticated}
        error={error}
      />
    );
  }

  return (
    <div className="min-h-screen bg-[#f7f8f6] text-slate-950">
      <Header
        profile={account?.profile}
        view={view}
        onNavigate={setView}
        onLogout={logout}
      />
      <main className="mx-auto max-w-7xl px-5 pb-16 pt-8 lg:px-8">
        {loading || !catalog || !account ? (
          <LoadingState />
        ) : error ? (
          <ErrorState message={error} onRetry={refresh} />
        ) : (
          <Dashboard
            view={view}
            catalog={catalog}
            account={account}
            onNavigate={setView}
            onRefresh={() => Promise.all([loadCatalog(), loadAccount()]).then(() => undefined)}
            onError={setError}
          />
        )}
      </main>
      <footer className="border-t border-slate-200 bg-white px-5 py-7 text-center text-sm text-slate-500">
        Elevate Studio · Pune · Move with intention
      </footer>
    </div>
  );
}

function AuthScreen({
  mode,
  onModeChange,
  onAuthenticated,
  error,
}: {
  mode: "login" | "register";
  onModeChange: (mode: "login" | "register") => void;
  onAuthenticated: (token: string) => Promise<void>;
  error: string;
}) {
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState("");

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setBusy(true);
    setFormError("");
    const data = new FormData(event.currentTarget);
    try {
      const response =
        mode === "login"
          ? await api.login({
              email: String(data.get("email")),
              password: String(data.get("password")),
            })
          : await api.register({
              full_name: String(data.get("full_name")),
              phone_e164: String(data.get("phone_e164")),
              email: String(data.get("email")),
              password: String(data.get("password")),
            });
      await onAuthenticated(response.access_token);
    } catch (reason) {
      setFormError(reason instanceof Error ? reason.message : "Unable to continue.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="flex min-h-screen items-center bg-[#10231f] px-5 py-10 text-white">
      <div className="mx-auto grid w-full max-w-6xl overflow-hidden rounded-[2rem] bg-[#17352e] shadow-2xl lg:grid-cols-[1.05fr_0.95fr]">
        <section className="relative hidden min-h-[680px] overflow-hidden p-12 lg:block">
          <div className="absolute inset-0 bg-[radial-gradient(circle_at_20%_20%,#c4f16a33,transparent_38%),linear-gradient(145deg,#17352e,#10231f)]" />
          <div className="relative flex h-full flex-col justify-between">
            <Brand light />
            <div className="max-w-md">
              <p className="mb-5 text-sm font-semibold uppercase tracking-[0.3em] text-lime-300">
                Studio membership
              </p>
              <h1 className="font-display text-6xl leading-[0.95] tracking-tight">
                A stronger rhythm for everyday life.
              </h1>
              <p className="mt-7 text-lg leading-8 text-emerald-50/70">
                Curated classes, thoughtful coaching and a studio community that
                keeps you moving forward.
              </p>
            </div>
            <p className="text-sm text-emerald-50/50">Personal training · Yoga · Group fitness</p>
          </div>
        </section>
        <section className="bg-white p-7 text-slate-950 sm:p-12">
          <div className="mb-12 lg:hidden"><Brand /></div>
          <div className="mb-9">
            <p className="text-sm font-semibold uppercase tracking-[0.25em] text-emerald-700">
              {mode === "login" ? "Welcome back" : "Join Elevate"}
            </p>
            <h2 className="font-display mt-3 text-4xl tracking-tight">
              {mode === "login" ? "Your next session starts here." : "Make space to feel good."}
            </h2>
          </div>
          <form onSubmit={submit} className="space-y-4">
            {mode === "register" && (
              <>
                <Field label="Full name" name="full_name" required />
                <Field label="Mobile number" name="phone_e164" placeholder="+919876543210" required />
              </>
            )}
            <Field label="Email address" name="email" type="email" required />
            <Field label="Password" name="password" type="password" minLength={12} required />
            {(formError || error) && <Alert>{formError || error}</Alert>}
            <button disabled={busy} className="button-primary mt-3 w-full">
              {busy ? "Please wait…" : mode === "login" ? "Sign in" : "Create account"}
            </button>
          </form>
          <button
            className="mt-7 w-full text-center text-sm font-semibold text-emerald-800 underline-offset-4 hover:underline"
            onClick={() => onModeChange(mode === "login" ? "register" : "login")}
          >
            {mode === "login" ? "New to Elevate? Create an account" : "Already a member? Sign in"}
          </button>
          <p className="mt-8 text-xs leading-5 text-slate-400">
            Your account is protected by secure session authentication. We never
            store your password in the browser.
          </p>
        </section>
      </div>
    </main>
  );
}

function Dashboard({
  view,
  catalog,
  account,
  onNavigate,
  onRefresh,
  onError,
}: {
  view: View;
  catalog: Catalog;
  account: Account;
  onNavigate: (view: View) => void;
  onRefresh: () => Promise<void>;
  onError: (message: string) => void;
}) {
  if (view === "schedule") {
    return <Schedule catalog={catalog} account={account} onRefresh={onRefresh} onError={onError} />;
  }
  if (view === "bookings") {
    return <Bookings catalog={catalog} account={account} onRefresh={onRefresh} onError={onError} />;
  }
  if (view === "finances") {
    return <Finances account={account} onError={onError} />;
  }
  if (view === "profile") {
    return <ProfileView profile={account.profile} />;
  }
  return <Overview catalog={catalog} account={account} onNavigate={onNavigate} />;
}

function Header({
  profile,
  view,
  onNavigate,
  onLogout,
}: {
  profile?: Profile;
  view: View;
  onNavigate: (view: View) => void;
  onLogout: () => void;
}) {
  const [open, setOpen] = useState(false);
  const items: [View, string][] = [
    ["overview", "Overview"],
    ["schedule", "Schedule"],
    ["bookings", "My bookings"],
    ["finances", "Finances"],
    ["profile", "Profile"],
  ];
  return (
    <header className="border-b border-slate-200 bg-white">
      <div className="mx-auto flex max-w-7xl items-center justify-between px-5 py-4 lg:px-8">
        <button onClick={() => onNavigate("overview")}><Brand /></button>
        <nav className="hidden items-center gap-1 lg:flex">
          {items.map(([key, label]) => (
            <button
              key={key}
              onClick={() => onNavigate(key)}
              className={`rounded-full px-4 py-2 text-sm font-semibold transition ${
                view === key ? "bg-emerald-950 text-white" : "text-slate-500 hover:bg-slate-100 hover:text-slate-950"
              }`}
            >
              {label}
            </button>
          ))}
        </nav>
        <div className="relative flex items-center gap-3">
          <span className="hidden text-right sm:block">
            <span className="block text-sm font-semibold">{profile?.full_name}</span>
            <span className="block text-xs text-slate-500">Member</span>
          </span>
          <button
            onClick={() => setOpen(!open)}
            className="flex h-10 w-10 items-center justify-center rounded-full bg-lime-200 font-display text-lg text-emerald-950"
          >
            {profile?.full_name.charAt(0).toUpperCase()}
          </button>
          {open && (
            <div className="absolute right-0 top-12 z-20 w-48 rounded-2xl border border-slate-200 bg-white p-2 shadow-xl">
              <button onClick={() => onNavigate("profile")} className="menu-item">Account details</button>
              <button onClick={onLogout} className="menu-item text-rose-700">Sign out</button>
            </div>
          )}
        </div>
      </div>
      <nav className="flex gap-1 overflow-x-auto px-4 pb-3 lg:hidden">
        {items.map(([key, label]) => (
          <button
            key={key}
            onClick={() => onNavigate(key)}
            className={`whitespace-nowrap rounded-full px-3 py-1.5 text-xs font-semibold ${
              view === key ? "bg-emerald-950 text-white" : "bg-slate-100 text-slate-500"
            }`}
          >
            {label}
          </button>
        ))}
      </nav>
    </header>
  );
}

function Overview({
  catalog,
  account,
  onNavigate,
}: {
  catalog: Catalog;
  account: Account;
  onNavigate: (view: View) => void;
}) {
  const upcoming = account.bookings
    .filter((booking) => booking.status === "confirmed" && booking.starts_at)
    .sort((a, b) => new Date(a.starts_at ?? "").getTime() - new Date(b.starts_at ?? "").getTime())[0];
  const activeCredits = account.passes
    .filter((pass) => pass.status === "active")
    .reduce((sum, pass) => sum + pass.credits_remaining, 0);
  return (
    <>
      <section className="relative overflow-hidden rounded-[2rem] bg-emerald-950 px-7 py-10 text-white sm:px-12 sm:py-14">
        <div className="absolute -right-20 -top-32 h-96 w-96 rounded-full bg-lime-300/15 blur-3xl" />
        <div className="relative max-w-2xl">
          <p className="text-sm font-semibold uppercase tracking-[0.3em] text-lime-300">Member space</p>
          <h1 className="font-display mt-4 text-5xl leading-[0.95] tracking-tight sm:text-6xl">
            Good to see you, {account.profile.full_name.split(" ")[0]}.
          </h1>
          <p className="mt-6 max-w-lg text-lg leading-8 text-emerald-50/70">
            Keep your momentum going. Your studio, sessions and progress are all in one place.
          </p>
          <button onClick={() => onNavigate("schedule")} className="button-lime mt-8">Find a session <span>↗</span></button>
        </div>
      </section>
      <div className="mt-6 grid gap-4 sm:grid-cols-3">
        <StatCard label="Active credits" value={String(activeCredits)} detail="Across your passes" icon="✦" />
        <StatCard label="Upcoming" value={upcoming ? "1" : "0"} detail={upcoming ? dateTime(upcoming.starts_at) : "Nothing booked yet"} icon="◷" />
        <StatCard label="Studio offering" value={String(catalog.services.length)} detail="Ways to move today" icon="＋" />
      </div>
      <section className="mt-10 grid gap-8 lg:grid-cols-[1.15fr_0.85fr]">
        <div>
          <SectionHeading eyebrow="Next up" title="Your upcoming sessions" action="View all" onAction={() => onNavigate("bookings")} />
          {upcoming ? <BookingCard booking={upcoming} compact /> : <EmptyState title="Your calendar is open" body="Explore the timetable and reserve your next session." action="Browse schedule" onAction={() => onNavigate("schedule")} />}
        </div>
        <div>
          <SectionHeading eyebrow="Explore" title="Find your practice" />
          <div className="grid gap-3">
            {catalog.services.slice(0, 3).map((service) => (
              <button key={service.id} onClick={() => onNavigate("schedule")} className="group flex items-center justify-between rounded-2xl border border-slate-200 bg-white p-4 text-left transition hover:-translate-y-0.5 hover:border-emerald-300 hover:shadow-lg">
                <span><span className="block font-semibold">{service.name}</span><span className="mt-1 block text-sm text-slate-500">{service.duration_minutes} min · {service.default_capacity} spots</span></span>
                <span className="text-xl text-emerald-700 transition group-hover:translate-x-1">↗</span>
              </button>
            ))}
          </div>
        </div>
      </section>
    </>
  );
}

function Schedule({
  catalog,
  account,
  onRefresh,
  onError,
}: {
  catalog: Catalog;
  account: Account;
  onRefresh: () => Promise<void>;
  onError: (message: string) => void;
}) {
  const [serviceFilter, setServiceFilter] = useState("all");
  const [selected, setSelected] = useState<Session | null>(null);
  const sessions = catalog.sessions.filter((session) => serviceFilter === "all" || session.service_id === serviceFilter);
  return (
    <>
      <PageIntro eyebrow="The timetable" title="Make time for your practice." body="Choose a class that fits your rhythm. Availability is checked live when you book." />
      <div className="mt-8 flex gap-2 overflow-x-auto pb-2">
        <FilterButton active={serviceFilter === "all"} onClick={() => setServiceFilter("all")}>All sessions</FilterButton>
        {catalog.services.map((service) => <FilterButton key={service.id} active={serviceFilter === service.id} onClick={() => setServiceFilter(service.id)}>{service.name}</FilterButton>)}
      </div>
      {sessions.length === 0 ? <EmptyState title="No upcoming sessions" body="Check back soon for the next timetable update." /> : (
        <div className="mt-5 grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {sessions.map((session) => <SessionCard key={session.id} session={session} onSelect={setSelected} />)}
        </div>
      )}
      {selected && <BookingModal session={selected} passes={account.passes} onClose={() => setSelected(null)} onRefresh={onRefresh} onError={onError} />}
    </>
  );
}

function Bookings({
  catalog,
  account,
  onRefresh,
  onError,
}: {
  catalog: Catalog;
  account: Account;
  onRefresh: () => Promise<void>;
  onError: (message: string) => void;
}) {
  const [rescheduling, setRescheduling] = useState<Booking | null>(null);
  const [busy, setBusy] = useState("");
  const upcoming = account.bookings.filter((booking) => booking.status === "confirmed" && new Date(booking.starts_at ?? 0) >= new Date());
  const past = account.bookings.filter((booking) => !upcoming.includes(booking));
  const cancel = async (booking: Booking) => {
    if (!window.confirm("Cancel this booking? The studio cancellation policy will be applied.")) return;
    setBusy(booking.id);
    try {
      await api.cancelBooking(booking.id, account.profile.id);
      await onRefresh();
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : "Unable to cancel booking.");
    } finally {
      setBusy("");
    }
  };
  return (
    <>
      <PageIntro eyebrow="Your movement" title="Bookings, beautifully simple." body="Manage your studio time. The studio's cancellation and rescheduling rules always apply." />
      <section className="mt-9">
        <SectionHeading eyebrow="Coming up" title="Upcoming sessions" />
        {upcoming.length ? <div className="grid gap-4 lg:grid-cols-2">{upcoming.map((booking) => <BookingCard key={booking.id} booking={booking} onCancel={cancel} onReschedule={setRescheduling} busy={busy === booking.id} />)}</div> : <EmptyState title="No upcoming bookings" body="A little consistency goes a long way." />}
      </section>
      <section className="mt-12">
        <SectionHeading eyebrow="Your history" title="Past sessions" />
        {past.length ? <div className="mt-4 divide-y divide-slate-200 rounded-2xl border border-slate-200 bg-white">{past.map((booking) => <BookingRow key={booking.id} booking={booking} />)}</div> : <p className="mt-4 text-sm text-slate-500">Your completed sessions will appear here.</p>}
      </section>
      {rescheduling && <RescheduleModal booking={rescheduling} sessions={catalog.sessions} onClose={() => setRescheduling(null)} onRefresh={onRefresh} onError={onError} />}
    </>
  );
}

function Finances({ account, onError }: { account: Account; onError: (message: string) => void }) {
  const [downloading, setDownloading] = useState("");
  const download = async (invoice: Invoice) => {
    setDownloading(invoice.invoice_id);
    try {
      const blob = await api.invoicePdf(invoice.invoice_id);
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `${invoice.invoice_number}.pdf`;
      link.click();
      URL.revokeObjectURL(url);
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : "Unable to download invoice.");
    } finally {
      setDownloading("");
    }
  };
  return (
    <>
      <PageIntro eyebrow="Your account" title="Passes and payments." body="A clear view of your credits, expiry dates and studio receipts." />
      <section className="mt-9">
        <SectionHeading eyebrow="Passes" title="Your active balances" />
        {account.passes.length ? <div className="mt-5 grid gap-4 md:grid-cols-2">{account.passes.map((pass) => <div key={pass.id} className="rounded-3xl bg-emerald-950 p-6 text-white"><div className="flex items-start justify-between"><div><p className="text-sm text-lime-300">{pass.status === "active" ? "Active pass" : "Pass history"}</p><h3 className="font-display mt-2 text-3xl">{pass.name}</h3></div><span className="rounded-full bg-white/10 px-3 py-1 text-xs">{pass.status}</span></div><div className="mt-10 flex items-end justify-between"><div><p className="text-4xl font-semibold">{pass.credits_remaining}</p><p className="text-sm text-emerald-50/60">sessions remaining</p></div><p className="text-right text-sm text-emerald-50/70">Expires<br /><strong className="text-white">{dateOnly(pass.expires_at)}</strong></p></div></div>)}</div> : <EmptyState title="No passes yet" body="Your studio pass balances will appear here." />}
      </section>
      <section className="mt-12 grid gap-10 lg:grid-cols-2">
        <div><SectionHeading eyebrow="Transactions" title="Payment history" />{account.payments.length ? <div className="mt-4 divide-y divide-slate-200 rounded-2xl border border-slate-200 bg-white">{account.payments.map((payment) => <div key={payment.id} className="flex items-center justify-between p-4"><span><span className="block font-semibold capitalize">{payment.kind}</span><span className="text-xs text-slate-500">{dateOnly(payment.received_at)} · {payment.method}</span></span><strong>{money(payment.amount_paise)}</strong></div>)}</div> : <p className="mt-4 text-sm text-slate-500">No payments recorded.</p>}</div>
        <div><SectionHeading eyebrow="Receipts" title="Invoices" />{account.invoices.length ? <div className="mt-4 divide-y divide-slate-200 rounded-2xl border border-slate-200 bg-white">{account.invoices.map((invoice) => <div key={invoice.invoice_id} className="flex items-center justify-between p-4"><span><span className="block font-semibold">{invoice.invoice_number}</span><span className="text-xs text-slate-500">{invoice.payment_status.replace("_", " ")} · {money(invoice.total_paise)}</span></span><button className="button-secondary px-3 py-2 text-xs" disabled={downloading === invoice.invoice_id} onClick={() => void download(invoice)}>{downloading === invoice.invoice_id ? "…" : "PDF"}</button></div>)}</div> : <p className="mt-4 text-sm text-slate-500">Your invoices will appear here.</p>}</div>
      </section>
    </>
  );
}

function ProfileView({ profile }: { profile: Profile }) {
  return (
    <>
      <PageIntro eyebrow="Your details" title="A space that is yours." body="Your profile is managed securely by Elevate Studio." />
      <div className="mt-9 max-w-2xl rounded-3xl border border-slate-200 bg-white p-6 sm:p-8">
        <div className="flex items-center gap-4 border-b border-slate-100 pb-6"><div className="flex h-16 w-16 items-center justify-center rounded-full bg-lime-200 font-display text-3xl text-emerald-950">{profile.full_name.charAt(0)}</div><div><h2 className="font-display text-3xl">{profile.full_name}</h2><p className="text-sm text-slate-500">Member since {dateOnly(profile.created_at)}</p></div></div>
        <dl className="mt-7 grid gap-6 sm:grid-cols-2"><Info label="Email address" value={profile.email ?? "Not provided"} /><Info label="Mobile number" value={profile.phone_e164} /><Info label="WhatsApp updates" value={profile.whatsapp_opt_in ? "Enabled" : "Not enabled"} /><Info label="Account status" value="Active member" /></dl>
      </div>
    </>
  );
}

function BookingModal({ session, passes, onClose, onRefresh, onError }: { session: Session; passes: PassBalance[]; onClose: () => void; onRefresh: () => Promise<void>; onError: (message: string) => void }) {
  const [passId, setPassId] = useState(passes.find((pass) => pass.status === "active" && pass.credits_remaining > 0)?.id ?? "");
  const [busy, setBusy] = useState(false);
  const activePasses = passes.filter((pass) => pass.status === "active" && pass.credits_remaining > 0);
  const book = async () => {
    if (!passId) return;
    setBusy(true);
    try {
      const profile = await api.profile();
      await api.createBooking({ session_id: session.id, customer_id: profile.id, member_pass_id: passId, idempotency_key: crypto.randomUUID() });
      await onRefresh();
      onClose();
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : "Unable to book this session.");
    } finally {
      setBusy(false);
    }
  };
  return <Modal title="Reserve your spot" onClose={onClose}><div className="rounded-2xl bg-slate-50 p-4"><p className="font-semibold">{session.service_name}</p><p className="mt-1 text-sm text-slate-500">{dateTime(session.starts_at)} · {session.trainer_name}</p></div>{session.available_seats < 1 ? <Alert>This session is currently full.</Alert> : activePasses.length === 0 ? <Alert>You need an active pass with available credits to book this session.</Alert> : <label className="mt-5 block text-sm font-semibold">Use pass<select value={passId} onChange={(event) => setPassId(event.target.value)} className="input mt-2">{activePasses.map((pass) => <option key={pass.id} value={pass.id}>{pass.name} · {pass.credits_remaining} credits left</option>)}</select></label>}<div className="mt-7 flex gap-3"><button onClick={onClose} className="button-secondary flex-1">Not now</button><button disabled={busy || !passId || session.available_seats < 1} onClick={() => void book()} className="button-primary flex-1">{busy ? "Reserving…" : "Confirm booking"}</button></div></Modal>;
}

function RescheduleModal({ booking, sessions, onClose, onRefresh, onError }: { booking: Booking; sessions: Session[]; onClose: () => void; onRefresh: () => Promise<void>; onError: (message: string) => void }) {
  const options = sessions.filter((session) => session.id !== booking.session_id && new Date(session.starts_at) > new Date() && session.available_seats > 0);
  const [sessionId, setSessionId] = useState(options[0]?.id ?? "");
  const [busy, setBusy] = useState(false);
  const submit = async () => {
    if (!sessionId) return;
    setBusy(true);
    try {
      await api.rescheduleBooking(booking.id, booking.customer_id, sessionId);
      await onRefresh();
      onClose();
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : "Unable to reschedule booking.");
    } finally {
      setBusy(false);
    }
  };
  return <Modal title="Move your session" onClose={onClose}><p className="text-sm text-slate-500">The backend will enforce the studio's rescheduling window and availability.</p>{options.length ? <select value={sessionId} onChange={(event) => setSessionId(event.target.value)} className="input mt-5">{options.map((session) => <option key={session.id} value={session.id}>{session.service_name} · {dateTime(session.starts_at)}</option>)}</select> : <Alert>No eligible replacement sessions are available.</Alert>}<div className="mt-7 flex gap-3"><button onClick={onClose} className="button-secondary flex-1">Keep booking</button><button disabled={busy || !sessionId} onClick={() => void submit()} className="button-primary flex-1">{busy ? "Moving…" : "Confirm move"}</button></div></Modal>;
}

function SessionCard({ session, onSelect }: { session: Session; onSelect: (session: Session) => void }) {
  const full = session.available_seats < 1;
  return <article className="rounded-3xl border border-slate-200 bg-white p-5 transition hover:-translate-y-1 hover:shadow-xl"><div className="flex items-start justify-between"><span className="rounded-full bg-lime-100 px-3 py-1 text-xs font-semibold text-emerald-900">{session.service_name}</span><span className={`text-xs font-semibold ${full ? "text-rose-600" : "text-emerald-700"}`}>{full ? "Full" : `${session.available_seats} spots`}</span></div><p className="mt-7 font-display text-2xl">{dateTime(session.starts_at)}</p><p className="mt-2 text-sm text-slate-500">with {session.trainer_name}</p><button disabled={full} onClick={() => onSelect(session)} className="button-primary mt-6 w-full disabled:cursor-not-allowed disabled:bg-slate-200 disabled:text-slate-500">{full ? "Join waitlist in studio" : "View session"}</button></article>;
}

function BookingCard({ booking, compact = false, onCancel, onReschedule, busy }: { booking: Booking; compact?: boolean; onCancel?: (booking: Booking) => void; onReschedule?: (booking: Booking) => void; busy?: boolean }) {
  return <article className={`rounded-3xl border border-slate-200 bg-white p-5 ${compact ? "" : "sm:p-6"}`}><div className="flex items-start justify-between gap-4"><div><p className="text-xs font-semibold uppercase tracking-[0.18em] text-emerald-700">{booking.service_name ?? "Studio session"}</p><h3 className="font-display mt-2 text-2xl">{dateTime(booking.starts_at)}</h3><p className="mt-1 text-sm text-slate-500">{booking.trainer_name ? `with ${booking.trainer_name}` : "Pass booking"} · {booking.booking_ref}</p></div><span className="rounded-full bg-emerald-50 px-3 py-1 text-xs font-semibold capitalize text-emerald-800">{booking.status}</span></div>{onCancel && onReschedule && <div className="mt-6 flex gap-2 border-t border-slate-100 pt-4"><button disabled={busy} onClick={() => onCancel(booking)} className="button-secondary flex-1 text-rose-700">{busy ? "Working…" : "Cancel"}</button><button disabled={busy} onClick={() => onReschedule(booking)} className="button-secondary flex-1">Reschedule</button></div>}</article>;
}

function BookingRow({ booking }: { booking: Booking }) {
  return <div className="flex items-center justify-between gap-4 p-4"><span><span className="block font-semibold">{booking.service_name ?? "Studio session"}</span><span className="text-xs text-slate-500">{dateTime(booking.starts_at)} · {booking.booking_ref}</span></span><span className="rounded-full bg-slate-100 px-3 py-1 text-xs font-semibold capitalize text-slate-600">{booking.status}</span></div>;
}

function Brand({ light = false }: { light?: boolean }) {
  return <span className={`font-display text-2xl tracking-tight ${light ? "text-white" : "text-emerald-950"}`}>elevate<span className="text-lime-500">.</span></span>;
}
function Field({ label, name, type = "text", ...props }: { label: string; name: string; type?: string; [key: string]: unknown }) {
  return <label className="block text-sm font-semibold text-slate-700">{label}<input name={name} type={type} className="input mt-2" {...props} /></label>;
}
function PageIntro({ eyebrow, title, body }: { eyebrow: string; title: string; body: string }) {
  return <div className="max-w-2xl"><p className="text-sm font-semibold uppercase tracking-[0.25em] text-emerald-700">{eyebrow}</p><h1 className="font-display mt-3 text-5xl leading-none tracking-tight">{title}</h1><p className="mt-5 text-lg leading-8 text-slate-500">{body}</p></div>;
}
function SectionHeading({ eyebrow, title, action, onAction }: { eyebrow: string; title: string; action?: string; onAction?: () => void }) {
  return <div className="mb-4 flex items-end justify-between gap-4"><div><p className="text-xs font-semibold uppercase tracking-[0.2em] text-emerald-700">{eyebrow}</p><h2 className="font-display mt-2 text-3xl tracking-tight">{title}</h2></div>{action && onAction && <button onClick={onAction} className="text-sm font-semibold text-emerald-800 hover:underline">{action} ↗</button>}</div>;
}
function StatCard({ label, value, detail, icon }: { label: string; value: string; detail: string; icon: string }) {
  return <div className="rounded-3xl border border-slate-200 bg-white p-5"><div className="flex items-center justify-between"><p className="text-sm font-semibold text-slate-500">{label}</p><span className="text-xl text-emerald-700">{icon}</span></div><p className="font-display mt-5 text-4xl">{value}</p><p className="mt-1 truncate text-sm text-slate-500">{detail}</p></div>;
}
function Info({ label, value }: { label: string; value: string }) { return <div><dt className="text-xs font-semibold uppercase tracking-wider text-slate-400">{label}</dt><dd className="mt-2 font-semibold">{value}</dd></div>; }
function FilterButton({ active, onClick, children }: { active: boolean; onClick: () => void; children: string }) { return <button onClick={onClick} className={`whitespace-nowrap rounded-full px-4 py-2 text-sm font-semibold ${active ? "bg-emerald-950 text-white" : "border border-slate-200 bg-white text-slate-600"}`}>{children}</button>; }
function EmptyState({ title, body, action, onAction }: { title: string; body: string; action?: string; onAction?: () => void }) { return <div className="rounded-3xl border border-dashed border-slate-300 bg-white p-8 text-center"><h3 className="font-display text-2xl">{title}</h3><p className="mx-auto mt-2 max-w-md text-sm leading-6 text-slate-500">{body}</p>{action && onAction && <button onClick={onAction} className="button-secondary mt-5">{action}</button>}</div>; }
function LoadingState() { return <div className="flex min-h-[55vh] items-center justify-center"><div className="text-center"><div className="mx-auto h-10 w-10 animate-spin rounded-full border-4 border-slate-200 border-t-emerald-800" /><p className="mt-4 text-sm text-slate-500">Preparing your studio space…</p></div></div>; }
function ErrorState({ message, onRetry }: { message: string; onRetry: () => Promise<void> }) { return <div className="mx-auto max-w-lg rounded-3xl border border-rose-200 bg-rose-50 p-8 text-center"><h2 className="font-display text-3xl text-rose-950">We hit a pause.</h2><p className="mt-3 text-sm text-rose-800">{message}</p><button onClick={() => void onRetry()} className="button-secondary mt-6">Try again</button></div>; }
function Alert({ children }: { children: string }) { return <div className="mt-4 rounded-xl border border-amber-200 bg-amber-50 p-3 text-sm leading-6 text-amber-900">{children}</div>; }
function Modal({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) { return <div className="fixed inset-0 z-30 flex items-end justify-center bg-slate-950/50 p-0 sm:items-center sm:p-5"><div className="max-h-[90vh] w-full max-w-md overflow-y-auto rounded-t-[2rem] bg-white p-6 shadow-2xl sm:rounded-[2rem]"><div className="flex items-center justify-between"><h2 className="font-display text-3xl">{title}</h2><button onClick={onClose} aria-label="Close" className="text-2xl text-slate-400 hover:text-slate-950">×</button></div>{children}</div></div>; }

createRoot(document.getElementById("root")!).render(
  window.location.pathname.startsWith("/admin") ? <AdminApp /> : <App />,
);
