function merged = mergeStructs(left, right)
%MERGESTRUCTS Overlay fields from right onto left.
merged = left;
names = fieldnames(right);
for idx = 1:numel(names)
    merged.(names{idx}) = right.(names{idx});
end
end
