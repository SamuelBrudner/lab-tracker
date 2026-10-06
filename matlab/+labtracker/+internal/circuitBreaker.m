function result = circuitBreaker(action, endpoint)
%CIRCUITBREAKER Per-endpoint pause after a transport failure, like the Python client's.
%
%   circuitBreaker('trip', url) opens the breaker for 30 seconds;
%   circuitBreaker('blocks', url) is true while it is open, so captures queue
%   offline at once instead of waiting on another connect timeout; after the
%   cooldown one capture probes the server again. circuitBreaker('close', url)
%   closes it after a success and circuitBreaker('reset') forgets every one.
persistent openUntil
cooldownMillis = 30000;
if isempty(openUntil) || strcmp(action, 'reset')
    openUntil = containers.Map('KeyType', 'char', 'ValueType', 'double');
end
result = false;
if nargin < 2
    return
end
key = labtracker.internal.textOf(endpoint);
if isempty(key)
    return
end
nowMillis = double(javaMethod('currentTimeMillis', 'java.lang.System'));
switch action
    case 'blocks'
        result = isKey(openUntil, key) && nowMillis < openUntil(key);
    case 'trip'
        openUntil(key) = nowMillis + cooldownMillis;
        result = true;
    case 'close'
        if isKey(openUntil, key)
            remove(openUntil, key);
        end
        result = true;
end
end
