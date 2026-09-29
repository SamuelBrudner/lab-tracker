function text = textOf(value)
%TEXTOF Return a char row for a char or string value, and '' for anything else.
text = '';
if isempty(value)
    return
end
if ischar(value)
    text = value(:).';
elseif isa(value, 'string')
    text = char(value);
end
end
