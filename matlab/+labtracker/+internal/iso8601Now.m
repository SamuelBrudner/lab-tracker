function value = iso8601Now()
%ISO8601NOW Current UTC timestamp in an API-friendly ISO 8601 form.
stamp = datetime('now', 'TimeZone', 'UTC');
stamp.Format = 'yyyy-MM-dd''T''HH:mm:ss.SSSXXX';
value = char(stamp);
end
