import { useId, useState } from 'react'

interface Props {
  checked: boolean
  onChange: (checked: boolean) => void
  disabled?: boolean
}

export const SAFE_HARBOR_HELP =
  'find clean or radio-edit versions. tracks without a verified clean match will be skipped.'

/**
 * Safe Harbor switch. A native checkbox with role="switch": Space toggles it, the
 * checked state is exposed as the switch state, and the whole label is clickable.
 * The help text is a tooltip shown on hover or keyboard focus (Escape hides it) and
 * stays linked as the switch's accessible description.
 */
export function SafeHarborToggle({ checked, onChange, disabled = false }: Props) {
  const helpId = useId()
  const [tipDismissed, setTipDismissed] = useState(false)
  return (
    <div
      className={`safe-harbor${checked ? ' is-on' : ''}${tipDismissed ? ' tip-dismissed' : ''}`}
      onKeyDown={(e) => {
        if (e.key === 'Escape') setTipDismissed(true)
      }}
      onMouseLeave={() => setTipDismissed(false)}
      onBlur={() => setTipDismissed(false)}
    >
      <label className="switch">
        <input
          type="checkbox"
          role="switch"
          className="switch-input"
          checked={checked}
          disabled={disabled}
          aria-describedby={helpId}
          onChange={(e) => onChange(e.target.checked)}
        />
        <span className="switch-track" aria-hidden="true">
          <span className="switch-thumb" />
        </span>
        <span className="switch-label">safe harbor</span>
        <span className="switch-state" aria-hidden="true">
          {checked ? 'enabled' : 'disabled'}
        </span>
      </label>
      <span id={helpId} role="tooltip" className="switch-tip">
        {SAFE_HARBOR_HELP}
      </span>
    </div>
  )
}
