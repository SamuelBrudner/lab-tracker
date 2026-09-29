function identity = credentialFreeRemote(remote)
%CREDENTIALFREEREMOTE A stable git remote identity with every credential removed.
%
%   The Python client's normalize_remote(sanitize_remote_url(remote)): drop
%   userinfo (keeping only an ssh login name), query and fragment, then the
%   scheme and any user@, turn ':' into '/', drop '.git', and lowercase.
%   https://user:token@GitHub.com/Lab/Repo.git -> github.com/lab/repo
remote = strtrim(labtracker.internal.textOf(remote));
identity = '';
if isempty(remote)
    return
end
sanitized = remote;
parts = regexp(remote, '^(?<scheme>[A-Za-z][A-Za-z0-9+.-]*)://(?<authority>[^/?#]*)(?<rest>.*)$', ...
    'names', 'once');
if ~isempty(parts)
    authority = parts.authority;
    at = find(authority == '@', 1, 'last');
    if ~isempty(at)
        userinfo = authority(1:at - 1);
        host = authority(at + 1:end);
        colon = find(userinfo == ':', 1);
        if isempty(colon)
            login = userinfo;
        else
            login = userinfo(1:colon - 1);
        end
        if any(strcmpi(parts.scheme, {'ssh', 'git+ssh', 'ssh+git'})) && ~isempty(login)
            authority = [login, '@', host];
        else
            authority = host;
        end
    end
    rest = regexprep(parts.rest, '[?#].*$', '');
    sanitized = [parts.scheme, '://', authority, rest];
end
cleaned = regexprep(sanitized, '^[a-zA-Z][a-zA-Z0-9+.-]*://', '');
if any(cleaned == '@') && isempty(strfind(sanitized, '://'))
    cleaned = cleaned(find(cleaned == '@', 1) + 1:end);
else
    cleaned = regexprep(cleaned, '^[^@/]+@', '');
end
cleaned = strrep(cleaned, ':', '/');
if numel(cleaned) >= 4 && strcmp(cleaned(end - 3:end), '.git')
    cleaned = cleaned(1:end - 4);
end
cleaned = regexprep(cleaned, '^/+|/+$', '');
identity = lower(cleaned);
end
