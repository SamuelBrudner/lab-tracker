\name{autotrack}
\alias{autotrack}
\alias{is_autotracking}
\alias{capture_file}
\alias{autotrack_status}
\title{Capture figures saved from R into Lab Tracker}
\description{
\code{autotrack()} wraps \code{ggplot2::ggsave()} (re-applied whenever ggplot2
is loaded again) and traces the \code{png()}, \code{jpeg()}, \code{tiff()},
\code{bmp()} and \code{pdf()} file devices: a device's file is captured when
\code{dev.off()} closes it. Each capture runs
\code{lt capture file <path> --kind figure --require-bound} in the background,
so only saves whose project comes from \code{project_id},
\code{LAB_TRACKER_PROJECT_ID} or the checkout's \code{lt_ids.json} are captured,
as staged evidence notes a person reviews. \code{svg()} is not captured (the
server refuses SVG uploads), nor is a device opened without a file name.

Nothing here raises an error into the session: a missing \code{lt}, an
unbound checkout, an unreachable server (the capture is queued offline) or a
failed capture is reported at most once per cause with \code{message()}.
Setting \code{LAB_TRACKER_AUTOTRACK=0} turns autotrack off.

\code{capture_file()} runs one explicit capture (not limited to bound
projects). \code{is_autotracking()} and \code{autotrack_status()} report the
hooks' state.
}
\usage{
autotrack(enabled = TRUE, project_id = NULL, metadata = list(), lt = NULL)
is_autotracking()
capture_file(path, kind = "figure", project_id = NULL, metadata = list(),
             require_bound = FALSE, wait = FALSE)
autotrack_status()
}
\arguments{
  \item{enabled}{\code{FALSE} removes every hook.}
  \item{project_id}{Optional project UUID for every capture.}
  \item{metadata}{Optional named list of scalar note metadata.}
  \item{lt}{Optional path to the \code{lt} executable, used after the
    \code{LAB_TRACKER_LT} environment variable and before \code{lt} on
    \code{PATH}.}
  \item{path}{The saved file to capture.}
  \item{kind}{Evidence kind label (default \code{"figure"}).}
  \item{require_bound}{Capture only into a bound project (the autotrack
    rule).}
  \item{wait}{\code{TRUE} waits for \code{lt} and returns its result;
    \code{FALSE} runs it in the background.}
}
\value{
\code{autotrack()} returns, invisibly, whether the hooks are installed.
\code{capture_file(wait = TRUE)} returns a list with the capture's
\code{action}, \code{reason} and \code{notices}.
}
\examples{
\dontrun{
labtracker::autotrack()
ggplot2::ggsave("figures/summary.png", plot)
}
}
