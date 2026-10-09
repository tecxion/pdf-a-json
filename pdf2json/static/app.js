// Envío por fetch, previsualización del resultado y descarga desde el blob.
//
// La descarga no puede salir directamente de la respuesta del POST: algunos
// navegadores reintentan con un GET sobre la misma URL, que aquí devuelve 405
// porque el JSON solo existe en esa respuesta. Con el blob se descarga sin
// segunda petición, y de paso se puede enseñar antes de guardarlo.
//
// Es mejora progresiva: sin JavaScript el formulario se envía de forma normal.
(function () {
  "use strict";

  var formulario = document.querySelector("form");
  var panel = document.getElementById("resultado");
  if (!formulario || !panel || typeof URL.createObjectURL !== "function") return;

  var pendiente = null; // { blob, nombre }

  function nombreDe(cabecera, porDefecto) {
    var coincidencia = /filename="([^"]+)"/.exec(cabecera || "");
    return coincidencia ? coincidencia[1] : porDefecto;
  }

  function descargar(blob, nombre) {
    var url = URL.createObjectURL(blob);
    var enlace = document.createElement("a");
    enlace.href = url;
    enlace.download = nombre;
    document.body.appendChild(enlace);
    enlace.click();
    enlace.remove();
    // Revocar de inmediato cancela la descarga en algunos navegadores.
    setTimeout(function () {
      URL.revokeObjectURL(url);
    }, 60000);
  }

  function elemento(etiqueta, clase, texto) {
    var nodo = document.createElement(etiqueta);
    if (clase) nodo.className = clase;
    if (texto !== undefined) nodo.textContent = texto;
    return nodo;
  }

  function resumirSecciones(secciones, lista, nivel) {
    secciones.forEach(function (seccion) {
      var fila = elemento("li", "arbol-nivel-" + Math.min(nivel, 3));
      fila.appendChild(
        elemento("span", "arbol-titulo", seccion.heading || "(sin título)")
      );
      var detalle = [];
      if (seccion.pages && seccion.pages.length) {
        detalle.push("pág. " + seccion.pages.join(", "));
      }
      if (seccion.tables && seccion.tables.length) {
        detalle.push(seccion.tables.length + " tabla(s)");
      }
      if (detalle.length) {
        fila.appendChild(elemento("span", "arbol-detalle", detalle.join(" · ")));
      }
      lista.appendChild(fila);
      if (seccion.children && seccion.children.length) {
        resumirSecciones(seccion.children, lista, nivel + 1);
      }
    });
  }

  function resumen(datos) {
    var caja = elemento("div", "resumen");
    var auto = datos.summary && datos.summary.auto;

    if (auto) {
      caja.appendChild(
        elemento(
          "p",
          "resumen-auto",
          "Modo elegido: " + auto.elegido + " — " + auto.motivo
        )
      );
    }

    if (datos.mode === "raw") {
      caja.appendChild(elemento("p", null, datos.pages.length + " páginas extraídas."));
      var muestra = (datos.pages[0] && datos.pages[0].text) || "";
      caja.appendChild(elemento("pre", "muestra", muestra.slice(0, 900)));
    } else if (datos.mode === "layout") {
      caja.appendChild(
        elemento("p", null, datos.summary.sections_found + " títulos detectados.")
      );
      var lista = elemento("ul", "arbol");
      resumirSecciones(datos.sections, lista, 1);
      caja.appendChild(lista);
    } else if (datos.mode === "schema") {
      var tabla = elemento("table", "campos");
      Object.keys(datos.fields).forEach(function (nombre) {
        var fila = elemento("tr");
        fila.appendChild(elemento("th", null, nombre));
        var valor = datos.fields[nombre];
        fila.appendChild(
          elemento(
            "td",
            valor === null ? "sin-valor" : null,
            valor === null ? "sin coincidencia" : valor
          )
        );
        tabla.appendChild(fila);
      });
      caja.appendChild(tabla);
    }

    var sinTexto = datos.summary && datos.summary.pages_without_text;
    if (sinTexto && sinTexto.length) {
      caja.appendChild(
        elemento(
          "p",
          "aviso",
          "Sin texto en " +
            sinTexto.length +
            " página(s): " +
            sinTexto.join(", ") +
            ". Probablemente estén escaneadas como imagen."
        )
      );
    }
    return caja;
  }

  function mostrar(blob, nombre, datos) {
    pendiente = { blob: blob, nombre: nombre };
    panel.textContent = "";
    panel.hidden = false;

    var cabecera = elemento("div", "resultado-cabecera");
    cabecera.appendChild(elemento("h2", null, "Resultado"));
    var boton = elemento("button", "primary", "Descargar " + nombre);
    boton.type = "button";
    boton.addEventListener("click", function () {
      descargar(pendiente.blob, pendiente.nombre);
    });
    cabecera.appendChild(boton);
    panel.appendChild(cabecera);

    if (datos) {
      panel.appendChild(resumen(datos));
      var detalles = elemento("details");
      detalles.appendChild(elemento("summary", null, "Ver el JSON completo"));
      detalles.appendChild(elemento("pre", "json", JSON.stringify(datos, null, 2)));
      panel.appendChild(detalles);
    } else {
      panel.appendChild(
        elemento("p", null, "Varios ficheros: el ZIP lleva un JSON por cada uno.")
      );
    }
    panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  function fallo(mensaje) {
    panel.textContent = "";
    panel.hidden = false;
    panel.appendChild(elemento("p", "error", mensaje));
  }

  // --- probador de reglas -------------------------------------------------
  // Dos pasos para no volver a subir el PDF cada vez que se retoca un patrón:
  // primero se extrae el texto una vez, después se prueban las reglas sobre él.
  var botonProbar = document.getElementById("probar-reglas");
  var panelPrueba = document.getElementById("prueba-reglas");
  var textoDelPdf = null;
  var pdfDelTexto = null;

  function reglasDelFormulario() {
    var datos = new FormData();
    formulario.querySelectorAll(".rule").forEach(function (fila) {
      datos.append("rule_name", fila.querySelector('[name="rule_name"]').value);
      datos.append("rule_kind", fila.querySelector('[name="rule_kind"]').value);
      datos.append("rule_pattern", fila.querySelector('[name="rule_pattern"]').value);
    });
    return datos;
  }

  function pintarPrueba(resultados) {
    panelPrueba.textContent = "";
    panelPrueba.hidden = false;
    var tabla = elemento("table", "campos");
    resultados.forEach(function (resultado) {
      var fila = elemento("tr");
      fila.appendChild(elemento("th", null, resultado.name));
      var celda = elemento("td", resultado.encontrado ? null : "sin-valor");
      if (resultado.encontrado) {
        celda.appendChild(elemento("strong", null, resultado.valor));
        if (resultado.contexto) {
          celda.appendChild(
            elemento("div", "contexto", "…" + resultado.contexto + "…")
          );
        }
      } else {
        celda.textContent = resultado.error || "sin coincidencia";
      }
      fila.appendChild(celda);
      tabla.appendChild(fila);
    });
    panelPrueba.appendChild(tabla);
  }

  function errorPrueba(mensaje) {
    panelPrueba.textContent = "";
    panelPrueba.hidden = false;
    panelPrueba.appendChild(elemento("p", "error", mensaje));
  }

  if (botonProbar && panelPrueba) {
    botonProbar.addEventListener("click", function () {
      var entrada = formulario.querySelector('input[type="file"]');
      var fichero = entrada.files && entrada.files[0];
      if (!fichero) {
        errorPrueba("Elige primero un PDF para probar las reglas sobre él.");
        return;
      }

      botonProbar.disabled = true;
      var original = botonProbar.textContent;
      botonProbar.textContent = "Probando…";

      var texto;
      if (textoDelPdf !== null && pdfDelTexto === fichero) {
        texto = Promise.resolve(textoDelPdf);
      } else {
        var subida = new FormData();
        subida.append("files", fichero);
        texto = fetch("/reglas/texto", { method: "POST", body: subida }).then(
          function (respuesta) {
            return respuesta.json().then(function (cuerpo) {
              if (!respuesta.ok) {
                throw new Error(cuerpo.error || "No se pudo leer el PDF.");
              }
              textoDelPdf = cuerpo.texto;
              pdfDelTexto = fichero;
              return cuerpo.texto;
            });
          }
        );
      }

      texto
        .then(function (contenido) {
          var datos = reglasDelFormulario();
          datos.append("texto", contenido);
          return fetch("/reglas/probar", { method: "POST", body: datos });
        })
        .then(function (respuesta) {
          return respuesta.json().then(function (cuerpo) {
            if (!respuesta.ok) throw new Error(cuerpo.error || "No se pudo probar.");
            pintarPrueba(cuerpo.resultados);
          });
        })
        .catch(function (error) {
          errorPrueba(error.message);
        })
        .finally(function () {
          botonProbar.disabled = false;
          botonProbar.textContent = original;
        });
    });
  }

  formulario.addEventListener("submit", function (evento) {
    evento.preventDefault();

    var boton = formulario.querySelector('button[type="submit"]');
    var textoOriginal = boton.textContent;
    boton.disabled = true;
    boton.textContent = "Convirtiendo…";
    panel.hidden = true;

    fetch(formulario.action, {
      method: "POST",
      body: new FormData(formulario),
      headers: { Accept: "application/json" },
    })
      .then(function (respuesta) {
        var nombre = nombreDe(
          respuesta.headers.get("content-disposition"),
          "pdf2json.json"
        );
        var tipo = respuesta.headers.get("content-type") || "";

        if (!respuesta.ok) {
          return respuesta.json().then(function (cuerpo) {
            fallo(cuerpo.error || "Error al convertir.");
          });
        }
        if (tipo.indexOf("application/zip") === 0) {
          return respuesta.blob().then(function (blob) {
            mostrar(blob, nombre, null);
          });
        }
        return respuesta.blob().then(function (blob) {
          return blob.text().then(function (texto) {
            mostrar(blob, nombre, JSON.parse(texto));
          });
        });
      })
      .catch(function (error) {
        fallo("No se pudo convertir: " + error.message);
      })
      .finally(function () {
        boton.disabled = false;
        boton.textContent = textoOriginal;
      });
  });
})();
