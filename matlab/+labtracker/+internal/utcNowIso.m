function value = utcNowIso()
%UTCNOWISO Current UTC time as ISO 8601, e.g. 2026-09-28T12:34:56.123456+00:00.
%
%   The same shape Python's datetime.isoformat() writes for the client's own
%   timestamps, built with java.time so it also runs under GNU Octave.
utc = javaMethod('of', 'java.time.ZoneOffset', 'Z');
formatter = javaMethod('ofPattern', 'java.time.format.DateTimeFormatter', ...
    'yyyy-MM-dd''T''HH:mm:ss.SSSSSSxxx');
value = char(formatter.withZone(utc).format(javaMethod('now', 'java.time.Instant')));
end
