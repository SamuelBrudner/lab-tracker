function session = activeSession()
%ACTIVESESSION The active session for captures, or [] when none is set or it expired.
%
%   LAB_TRACKER_SESSION_ID (a UUID or link code) pins one for this process;
%   otherwise `lt session use` records one, with its project and an expiry,
%   in <checkout>/.lab-tracker/session.json (LAB_TRACKER_SESSION_CONTEXT
%   names another file). The checkout is the one MATLAB's current folder is
%   in, as for the Python client. Reading is fail-soft: anything unreadable,
%   malformed or past its expires_at means no session.
session = [];
try
    fromEnv = strtrim(getenv('LAB_TRACKER_SESSION_ID'));
    if ~isempty(fromEnv)
        id = labtracker.internal.sessionId(fromEnv);
        if ~isempty(id)
            session = sessionStruct(id, '', 'env');
        end
        return
    end
    path = strtrim(getenv('LAB_TRACKER_SESSION_CONTEXT'));
    if isempty(path)
        root = labtracker.internal.captureCheckout(pwd);
        if isempty(root)
            root = labtracker.internal.canonicalPath(pwd);
        end
        path = fullfile(root, '.lab-tracker', 'session.json');
    end
    payload = labtracker.internal.readJsonFile(path);
    id = labtracker.internal.sessionId(labtracker.internal.jsonText(payload, 'session_id'));
    if isempty(id)
        return
    end
    expiresAt = labtracker.internal.jsonText(payload, 'expires_at');
    if ~isempty(expiresAt) && epochMillis(expiresAt) <= ...
            double(javaMethod('currentTimeMillis', 'java.lang.System'))
        return
    end
    session = sessionStruct(id, labtracker.internal.jsonText(payload, 'project_id'), 'checkout');
catch
    session = [];
end
end

function session = sessionStruct(id, projectId, source)
session = struct( ...
    'session_id', id, ...
    'link_code', labtracker.internal.sessionLinkCode(id), ...
    'project_id', projectId, ...
    'source', source);
end

function millis = epochMillis(text)
% An ISO 8601 time with an offset or Z; one without is taken as UTC.
try
    parsed = javaMethod('parse', 'java.time.OffsetDateTime', text);
    millis = double(parsed.toInstant().toEpochMilli());
catch
    local = javaMethod('parse', 'java.time.LocalDateTime', text);
    utc = javaMethod('of', 'java.time.ZoneOffset', 'Z');
    millis = double(local.toInstant(utc).toEpochMilli());
end
end
