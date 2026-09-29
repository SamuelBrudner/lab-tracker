function shown = warnOnce(key, identifier, message)
%WARNONCE Issue a warning at most once per MATLAB session for each cause (key).
%
%   warnOnce('reset') forgets every cause (for tests).
persistent seen
if isempty(seen) || (nargin == 1 && strcmp(key, 'reset'))
    seen = containers.Map('KeyType', 'char', 'ValueType', 'logical');
end
shown = false;
if nargin == 1
    return
end
if isKey(seen, key)
    return
end
seen(key) = true;
warning(identifier, '%s', message);
shown = true;
end
