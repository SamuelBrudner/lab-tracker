function target = sessionTarget(session, projectId, action)
%SESSIONTARGET The session id a capture in projectId may declare as its target, or ''.
%
%   Mirrors the Python client's session_target: LAB_TRACKER_SESSION_ID is a
%   per-process choice and always targets; a checkout session targets only
%   captures filed into the project recorded with it (one recorded without a
%   project targets nothing). A session the server refused for a project is
%   remembered for this MATLAB session:
%   sessionTarget(session, projectId, 'refused') records that refusal.
persistent refused
if isempty(refused)
    refused = containers.Map('KeyType', 'char', 'ValueType', 'logical');
end
target = '';
if isempty(session) || ~isstruct(session) || isempty(session.session_id)
    return
end
key = [session.session_id, '|', labtracker.internal.textOf(projectId)];
if nargin >= 3 && strcmp(action, 'refused')
    refused(key) = true;
    return
end
if isKey(refused, key)
    return
end
if strcmp(session.source, 'env') || strcmp(session.project_id, labtracker.internal.textOf(projectId))
    target = session.session_id;
end
end
