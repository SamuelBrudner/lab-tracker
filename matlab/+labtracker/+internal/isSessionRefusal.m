function tf = isSessionRefusal(err)
%ISSESSIONREFUSAL True when the server refused the capture's declared session target.
%
%   The two refusals the Python client retries without the session: a session
%   in another project (HTTP 422) or one that does not exist (HTTP 404).
tf = false;
try
    message = strtrim(err.message);
    tf = (strcmp(err.identifier, 'labtracker:validation') && ...
            strcmp(message, 'Target must belong to the same project.')) || ...
        (strcmp(err.identifier, 'labtracker:api') && strcmp(message, 'Session does not exist.'));
catch
    tf = false;
end
end
