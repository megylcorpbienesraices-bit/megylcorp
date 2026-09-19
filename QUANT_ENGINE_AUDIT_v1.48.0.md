# AUDITORÍA DEL MOTOR · ITM QUANT v1.48.0

## Veredicto

v1.47.0 arregló el grosor de las barras y dio por cerrada la legibilidad. No lo
estaba. Lo que esta auditoría encuentra es que varias decisiones de
representación eran **correctas por separado y equivocadas para lo que se estaba
mirando**, y que ninguna prueba podía detectarlo porque todas medían geometría
—grosor, hueco, ocupación— y ninguna medía **si la lectura era posible**.

---

## 1 · Legible no es lo mismo que útil

v1.47.0 certificó que ninguna barra bajaba de 5 px y que ninguna se solapaba. Las
barras del perfil por strike cumplían las dos cosas… agrupando tres strikes en
cada una. Geométricamente impecable; inútil para la pregunta que responde el
panel, que es **en qué strike está el muro**.

La prueba que faltaba no es de píxeles: es de **unidad de lectura**. Un perfil
por strike tiene que dibujar un strike por barra, y si no caben, lo que hay que
crecer es el panel, no juntar strikes. La agrupación adaptativa sigue siendo
correcta para un histograma temporal —«los últimos cinco minutos» es una unidad
legítima— y es errónea aquí. La misma técnica, dos veredictos, según qué
signifique el eje.

## 2 · Una rejilla no es un campo

El mapa de intervalos se dibujaba celda a celda: rectángulos duros con huecos
negros entre ellos. Cada celda era correcta. El conjunto no era un mapa de calor
sino **una tabla pintada**, y obligaba a leer celda por celda exactamente lo que
hay que leer como zona.

La diferencia no es estética. Un campo continuo permite ver **una cresta que se
desplaza en el tiempo**; una rejilla obliga a reconstruir ese desplazamiento
mentalmente, celda por celda. El dato era el mismo; la lectura, imposible.

Y una celda sin observación se estaba pintando como un cero. No lo es: es un
hueco, y un hueco en mitad de una cresta la parte en dos. Interpolar desde las
vecinas no inventa dato —el dato sigue en `raw()` y el hover sólo enseña el
medido—: reconstruye la continuidad que el muestreo rompió.

## 3 · El suelo de ruido no es un ajuste de gusto

La normalización por rango reparte los percentiles de forma uniforme. Sin suelo,
**la mitad del lienzo sale a media opacidad**, y el resultado es un bloque macizo
donde no se distingue ninguna concentración: lo contrario de un mapa.

Es la misma lección que el grosor de las barras en v1.47.0, en otra dimensión:
una normalización que reparte bien no garantiza que se lea bien. Hay que decidir
además **qué parte del rango es fondo**, y eso no sale de la estadística.

## 4 · El mismo dato, dos tratamientos

TRACE tenía su umbral y su curva; el Interval Map de la sección, los suyos. El
mismo dato se veía distinto en dos pantallas del mismo programa.

Es el patrón que esta serie de releases lleva encontrando release tras release
—tres Walls en v1.44, dos mapas de autoridad en v1.45, tres aritméticas de grosor
en v1.47— con una forma nueva: **la misma autoridad escrita en dos sitios siempre
deriva**. Aquí no derivaba en los números, que es lo que se venía vigilando.
Derivaba en el aspecto, y nadie estaba mirando eso.

## 5 · El color estaba ocupado

Las marcas de prima codificaban CALL/PUT en verde y rojo. El precio usa ese mismo
verde y ese mismo rojo para otra cosa. Una marca sobre un tramo de su propio
color desaparecía dentro de él.

Codificar dos variables distintas con el mismo canal visual no falla siempre:
falla cuando coinciden, que es precisamente cuando hay algo que mirar. El sentido
se ha movido a la **forma** —la flecha— y el color queda libre para decir «esto
es una marca de flujo», que es lo único que tiene que decir.

## 6 · Un cero que era un nombre equivocado

`norm_dark_flow` buscaba el volumen en una lista fija de nombres y devolvía `0.0`
cuando ninguno aparecía. La sección publicaba «608 intervalos · 0.0 acc»: seis
mil ochocientos intervalos descargados y una afirmación falsa sobre el mercado.

El modo de fallo es el peor posible —**un valor plausible**— y no había forma de
distinguirlo desde la pantalla. La corrección tiene dos mitades y las dos hacen
falta: el valor ausente pasa a ser `None`, y el campo se **deriva de la propia
respuesta** dejando escrito cuál se usó. Descubrir bajo qué nombre llega un dato
no es adivinar el dato; adivinarlo sería inventarse el valor, que es lo que hacía
el cero.

---

## Lo que esta auditoría NO puede afirmar

- Que `dark-pool-levels` devuelva 200: sigue sin haber salida hacia el proveedor.
- Que el Interval Map con datos del proveedor se vea como aquí: en este entorno
  no hay credenciales. Lo que sí se afirma es que TRACE y la sección pasan ahora
  por **la misma ruta**, así que lo que se vea en una se verá en la otra.
