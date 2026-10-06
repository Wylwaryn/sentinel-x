import { useMemo } from "react";
import { LineChart, Line, XAxis, YAxis, Tooltip, CartesianGrid, ResponsiveContainer } from "recharts";
import { heure, valeur } from "./format.js";

// Une courbe pour UNE grandeur de la table mesure (cle = "temperature_c", "humidite_pct", "gaz_brut" ou "pir")
// marches = courbe en escalier 0/1 (événements PIR)
// ecartMax = au-delà de cet écart entre deux points (ms), le boîtier était coupé : la courbe s'interrompt
export default function Courbe({ titre, unite, cle, couleur, mesures, ecartMax, marches = false }) {
  const derniere = mesures.at(-1);
  const donnees = useMemo(() => {
    const out = [];
    for (const m of mesures) {
      const prec = out.at(-1);
      if (prec && m.t - prec.t > ecartMax) out.push({ t: prec.t + 1, [cle]: null });
      out.push(m);
    }
    return out;
  }, [mesures, ecartMax, cle]);
  const affichage = marches
    ? (v) => (v ? "Mouvement" : "Calme")
    : (v) => valeur(v, unite, cle === "gaz_brut" ? 0 : 1);

  return (
    <section className="panneau">
      <h2>{titre}</h2>
      <div className="valeur">{derniere ? affichage(derniere[cle]) : "—"}</div>

      <ResponsiveContainer width="100%" height={marches ? 90 : 160}>
        <LineChart data={donnees}>
          <CartesianGrid stroke="#2c2c2a" vertical={false} />
          <XAxis dataKey="t" type="number" scale="time" domain={["dataMin", "dataMax"]}
            tickFormatter={(t) => heure(t)} stroke="#898781" fontSize={12} minTickGap={40} />
          <YAxis domain={marches ? [0, 1] : ["auto", "auto"]} ticks={marches ? [0, 1] : undefined}
            stroke="#898781" fontSize={12} width={45} />
          <Tooltip
            labelFormatter={(t) => heure(t)}
            formatter={(v) => [affichage(v), titre]}
            contentStyle={{ background: "#1a1a19", border: "1px solid #383835" }}
          />
          {/* isAnimationActive=false : sinon la courbe "rebondit" à chaque nouvelle mesure */}
          <Line dataKey={cle} type={marches ? "stepAfter" : "linear"} stroke={couleur} strokeWidth={2}
            dot={false} isAnimationActive={false} connectNulls={false} />
        </LineChart>
      </ResponsiveContainer>
    </section>
  );
}
