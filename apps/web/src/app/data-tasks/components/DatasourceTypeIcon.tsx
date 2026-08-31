import { getDatasourceIconSrc, getDatasourceVisualMeta } from "../datasource-metadata";

type DatasourceTypeIconProps = {
  typeName?: string;
  className?: string;
  iconClassName?: string;
};

export function DatasourceTypeIcon({
  typeName,
  className = "flex shrink-0 items-center justify-center rounded-xl border border-border bg-white",
  iconClassName = "h-7 w-7 object-contain",
}: DatasourceTypeIconProps) {
  const iconSrc = getDatasourceIconSrc(typeName);
  const visual = getDatasourceVisualMeta(typeName);

  if (iconSrc) {
    return (
      <span className={className}>
        <img src={iconSrc} alt="" className={iconClassName} />
      </span>
    );
  }

  return (
    <span className={[className, "text-xs font-bold", visual.accentClass].join(" ")}>
      {visual.mark}
    </span>
  );
}
