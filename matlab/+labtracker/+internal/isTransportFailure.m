function tf = isTransportFailure(err)
%ISTRANSPORTFAILURE True when the server never answered (connect, DNS, timeout).
%
%   An error the client raised after the server answered (labtracker:api,
%   labtracker:validation) or before sending (labtracker:auth, a bad file) is
%   not a transport failure: queueing it offline would only fail again later.
tf = false;
try
    if isa(err, 'matlab.net.http.HTTPException')
        tf = true;
        return
    end
    identifier = lower(err.identifier);
    if strncmp(identifier, 'labtracker:', numel('labtracker:'))
        return
    end
    text = [identifier, ' ', lower(err.message)];
    tf = ~isempty(regexp(text, ['webservices|connection|could not connect|connect(ion)? ', ...
        'refused|timed out|timeout|unknownhost|unknown host|could not resolve|', ...
        'network is unreachable|no route to host'], 'once'));
catch
    tf = false;
end
end
