function metadata = captureMetadata(extra, session, includeRun)
%CAPTUREMETADATA Caller metadata plus host, git run and active-session keys.
%
%   The same layering as the Python client's figure metadata: the caller's
%   own keys, then capture_host_label/capture_platform, then run_* git facts,
%   then capture_session_id/_link_code/_source for an active session.
if nargin < 3
    includeRun = true;
end
if isstruct(extra) && isscalar(extra)
    metadata = extra;
else
    metadata = struct();
end
metadata = labtracker.internal.mergeStructs(metadata, labtracker.internal.hostMetadata());
if includeRun
    metadata = labtracker.internal.mergeStructs(metadata, labtracker.internal.runMetadata());
end
if ~isempty(session)
    metadata.capture_session_id = session.session_id;
    metadata.capture_session_link_code = session.link_code;
    metadata.capture_session_source = session.source;
end
end
