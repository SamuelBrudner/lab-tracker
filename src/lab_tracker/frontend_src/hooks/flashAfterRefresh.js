// After a mutation has succeeded, refresh the project view and report the
// outcome. A failed refresh must not be reported as a failed mutation (the
// write already happened), so it is flashed as the success plus the refresh
// error rather than thrown into the mutation's catch.
async function flashAfterRefresh({ refresh, setFlash, success }) {
  try {
    await refresh();
  } catch (err) {
    setFlash(
      "",
      `${success.replace(/\.$/, "")}, but the project view could not be refreshed: ${
        err?.message || "unknown error"
      }`
    );
    return;
  }
  setFlash(success);
}

export { flashAfterRefresh };
